import sys
import re
import logging
import io
from parse_vcf import VcfReader, VcfHeader, VcfRecord 
from .dbsnp_filter import dbSnpFilter 
from .gnomad_filter import GnomadFilter 
from .vcf_filter import VcfFilter 
from .vep_filter import VepFilter  
from .cadd_filter import CaddFilter
from .sample_filter import SampleFilter
from .ped_file import PedFile, Family, Individual, PedError
from .family_filter import FamilyFilter, ControlFilter
from .family_filter import RecessiveFilter, DominantFilter, DeNovoFilter
from .burden_counter import BurdenCounter

class VaseRunner(object):

    def __init__(self, args):

        self.args = args
        self._set_logger()
        self.input = VcfReader(self.args.input)
        self.prev_cadd_phred = False
        self.prev_cadd_raw = False
        self._get_prev_annotations()
        self.out = self.get_output()
        self.vcf_filters = self.get_vcf_filter_classes()
        self.cadd_filter = self.get_cadd_filter()
        self.ped = None
        if args.ped:
            self.ped = PedFile(args.ped)
        self.csq_filter = None
        if args.csq is not None:
            self.csq_filter = VepFilter(
                                csq=args.csq, canonical=args.canonical,
                                biotypes=args.biotypes, 
                                in_silico=args.missense_filters,
                                filter_unpredicted=args.filter_unpredicted,
                                keep_any_damaging=args.keep_if_any_damaging,
                                filter_flagged_features=args.flagged_features)
        self.sample_filter = None
        self.burden_counter = None
        if args.burden_counts:
            if args.n_cases or args.n_controls:
                self.logger.warn("--n_cases and --n_controls arguments are " +
                                 "ignored when using --burden_counter.")
            self.burden_counter = BurdenCounter(self.input, args.burden_counts,
                                                is_gnomad=args.gnomad_burden,
                                                cases=args.cases, 
                                                controls=args.controls,
                                                gq=args.gq, dp=args.dp, 
                                                het_ab=args.het_ab,
                                                hom_ab=args.hom_ab,)
        elif args.cases or args.controls:
            self.sample_filter = SampleFilter(self.input, cases=args.cases, 
                                              controls=args.controls, 
                                              n_cases=args.n_cases,
                                              n_controls=args.n_controls, 
                                              gq=args.gq, dp=args.dp, 
                                              het_ab=args.het_ab,
                                              hom_ab=args.hom_ab,)
        self.de_novo_filter = None
        self.dominant_filter = None
        self.recessive_filter = None
        self.family_filter = None
        self.control_filter = None
        self.variant_cache = VariantCache()
        self.use_cache = False
        self.prog_interval = args.prog_interval
        self.log_progress = args.log_progress
        self.report_fhs = self.get_report_filehandles()
        if args.de_novo:
            self._get_de_novo_filter()
        if args.biallelic or args.singleton_recessive:
            self._get_recessive_filter()
        if args.dominant or args.singleton_dominant:
            self._get_dominant_filter()
        self._check_got_inherit_filter()
        self.var_written = 0
        self.var_filtered = 0

    def run(self):
        ''' Run VCF filtering/annotation using args from bin/vase.py '''
        self.logger.info('Starting variant processing')
        self.print_header()
        var_count = 0
        prog_string = ''
        for record in self.input.parser:
            self.process_record(record)
            var_count += 1
            if not self.args.quiet and var_count % self.prog_interval == 0:
                n_prog_string = ('{:,} variants processed, '.format(var_count) +
                               '{:,} filtered, {:,} written... at pos {}:{}'
                               .format(self.var_filtered, self.var_written,
                                       record.CHROM, record.POS))
                if self.log_progress:
                    self.logger.info(n_prog_string)
                else:
                    n_prog_string = '\r' + n_prog_string
                    if len(prog_string) > len(n_prog_string):
                        sys.stderr.write('\r' + ' ' * len(prog_string) )
                    prog_string = n_prog_string
                    sys.stderr.write(prog_string)
        self.finish_up()
        if prog_string:
            sys.stderr.write('\r' + '-' * len(prog_string) + '\n')
        self.logger.info('Finished processing {:,} {}.' 
                             .format(var_count, self._var_or_vars(var_count)))
        self.logger.info('{:,} {} filtered.' 
                         .format(self.var_filtered, 
                                 self._var_or_vars(self.var_filtered))) 
        self.logger.info('{:,} {} written.' 
                         .format(self.var_written, 
                                 self._var_or_vars(self.var_written))) 
        if self.out is not sys.stdout:
            self.out.close()

    def process_record(self, record):
        if self.filter_global(record):
            self.var_filtered += 1
            return
        filter_alleles, filter_csq = self.filter_alleles_external(record)
        if sum(filter_alleles) == len(filter_alleles): 
            #all alleles should be filtered
            self.var_filtered += 1
            return
        if self.sample_filter:
            for i in range(1, len(record.ALLELES)):
                r = self.sample_filter.filter(record, i)
                if r:
                    filter_alleles[i-1] = True
                if sum(filter_alleles) == len(filter_alleles): 
                    #all alleles should be filtered
                    self.var_filtered += 1
                    return
        dom_filter_alleles = list(filter_alleles)
        if self.control_filter:
            for i in range(1, len(record.ALLELES)):
                if dom_filter_alleles[i-1]: #no need to filter again
                    continue
                r = self.control_filter.filter(record, i)
                if r:
                    dom_filter_alleles[i-1] = True
        denovo_hit = False
        dom_hit = False
        recessive_hit = False
        if self.dominant_filter:
            dom_hit = self.dominant_filter.process_record(record,
                                                          dom_filter_alleles, 
                                                          filter_csq)
        if self.de_novo_filter:
            denovo_hit = self.de_novo_filter.process_record(record,
                                                            dom_filter_alleles,
                                                            filter_csq)
        if self.recessive_filter:
            recessive_hit = self.recessive_filter.process_record(record, 
                                                    filter_alleles, filter_csq) 
        if self.use_cache:
            if denovo_hit or dom_hit or recessive_hit:
                keep_record_anyway = False
                if self.args.min_families < 2:
                    keep_record_anyway = denovo_hit or dom_hit
                self.variant_cache.add_record(record, keep_record_anyway)
            else:
                self.variant_cache.check_record(record)
                self.var_filtered += 1
            if self.variant_cache.output_ready:
                self.output_cache()
        elif self.de_novo_filter or self.dominant_filter:
            if denovo_hit or dom_hit:
                self.out.write(str(record) + '\n')
                self.var_written += 1
        else:
            if sum(filter_alleles) == len(filter_alleles): 
                self.var_filtered += 1
                return
            self.var_written += 1
            if self.burden_counter:
                self.burden_counter.count(record, filter_alleles, filter_csq)
            self.out.write(str(record) + '\n')
    
    def output_cache(self, final=False):
        keep_ids = set()
        burden_vars = dict #dict of inheritance model to segregants
        if self.recessive_filter:
            vid_to_seg = self.recessive_filter.process_potential_recessives(
                                                                   final=final)
            keep_ids.update(vid_to_seg.keys())
            if self.burden_counter:
                burden_vars['recessive'] = vid_to_seg
        if self.dominant_filter and self.args.min_families > 1:
            vid_to_seg = self.dominant_filter.process_dominants(final=final)
            keep_ids.update(vid_to_seg.keys())
            if self.burden_counter:
                burden_vars['dominant'] = vid_to_seg
        if self.de_novo_filter and self.args.min_families > 1:
            vid_to_seg = self.de_novo_filter.process_de_novos(final=final)
            keep_ids.update(vid_to_seg.keys())
            if self.burden_counter:
                burden_vars['de_novo'] = vid_to_seg
        if final:
            self.variant_cache.add_cache_to_output_ready()
        for var in self.variant_cache.output_ready:
            if var.can_output or var.var_id in keep_ids:
                self.out.write(str(var.record) + '\n')
                self.var_written += 1
            else:
                self.var_filtered += 1
        if self.burden_counter:
            self._burden_from_cache(burden_vars)
        self.variant_cache.output_ready = []

    def _burden_from_cache(self, model_to_vars):
        for model in ('dominant', 'de_novo'):
            if model in model_to_vars:
                for v in model_to_vars[model]:
                    for seg in model_to_vars[model][v]:
                        self.burden_counter.count_samples(seg.record, 
                                                          seg.features,
                                                          seg.allele, 1)
        # we count recessives last as the max per allele (2) is higher than for
        # dom/de novos (otherwise max per sample could get lowered to 1)
        if 'recessive' in model_to_vars:
            for v in model_to_vars['recessive']:
                for seg in model_to_vars['recessive'][v]:
                    self.burden_counter.count_samples(seg.record, seg.features,
                                                      seg.allele, 2)

    def finish_up(self):
        if self.use_cache:
            self.output_cache(final=True)
        if self.burden_counter:
            self.burden_counter.output_counts()
        for fh in self.report_fhs.values():
            if fh is not None:
                fh.close()

    def filter_alleles_external(self, record):
        ''' 
            Return True or False for each allele indicating whether an 
            allele should be filtered based on information from VEP, 
            dbSNP, ClinVar or gnomAD.
        '''

        # remove_alleles indicates whether allele should be filtered; 
        # keep_alleles indicates whether allele should be kept, overriding any 
        # indications in remove_alleles (e.g. if labelled pathogenic in 
        # ClinVar)
        # remove_csq indicates for each VEP CSQ whether that CSQ should be
        # ignored
        remove_alleles = [False] * (len(record.ALLELES) -1)
        keep_alleles = [False] * (len(record.ALLELES) -1)
        matched_alleles = [False] * (len(record.ALLELES) -1)
        remove_csq = None
        #if allele is '*' should be set to filtered
        for i in range(1, len(record.ALLELES)):
            if record.ALLELES[i] == '*':
                remove_alleles[i-1] = True
        #check VCF's internal AF
        if self.args.af or self.args.min_af:
            r_alts = self.filter_on_af(record)
            self._set_to_true_if_true(remove_alleles, r_alts)
            if (not self.args.clinvar_path and 
                sum(remove_alleles) == len(remove_alleles)):
                # bail out now if no valid allele and not keeping clinvar
                # path variants - if using clinvar path we have to ensure we 
                # haven't got a path variant with a non-qualifying allele
                return remove_alleles, remove_csq
        #check functional consequences
        if self.csq_filter:
            r_alts, remove_csq = self.csq_filter.filter(record)
            self._set_to_true_if_true(remove_alleles, r_alts)
            if (not self.args.clinvar_path and 
                sum(remove_alleles) == len(remove_alleles)):
                # bail out now if no valid consequence and not keeping clinvar
                # path variants - if using clinvar path we have to ensure we 
                # haven't got a path variant with a non-qualifying consequence
                return remove_alleles, remove_csq
        if self.prev_cadd_phred and self.args.cadd_phred:
            r_alts = self.filter_on_existing_cadd_phred(record)
            self._set_to_true_if_true(remove_alleles, r_alts)
        if self.prev_cadd_raw and self.args.cadd_raw:
            r_alts = self.filter_on_existing_cadd_raw(record)
            self._set_to_true_if_true(remove_alleles, r_alts)
        if self.cadd_filter:
            r_alts = self.cadd_filter.annotate_or_filter(record)
            self._set_to_true_if_true(remove_alleles, r_alts)
            if (not self.args.clinvar_path and 
                sum(remove_alleles) == len(remove_alleles)):
                # bail out now if no valid consequence and not keeping clinvar
                # path variants - if using clinvar path we have to ensure we 
                # haven't got a path variant with a non-qualifying consequence
                return remove_alleles, remove_csq
        for f in self.vcf_filters:
            r, k, m = f.annotate_and_filter_record(record)
            # should only overwrite value of remove_alleles[i] or 
            # keep_alleles[i] with True, not False (e.g. if already set to 
            # be filtered because of a freq in ExAC we shouldn't set to 
            # False just because it is absent from dbSNP)
            self._set_to_true_if_true(remove_alleles, r)
            self._set_to_true_if_true(matched_alleles, m)
            self._set_to_true_if_true(keep_alleles, k)
        if self.prev_freqs:
            r,m = self.filter_on_existing_freq(record)
            self._set_to_true_if_true(remove_alleles, r)
            self._set_to_true_if_true(matched_alleles, m)
        if self.prev_builds:
            r,m = self.filter_on_existing_build(record)
            self._set_to_true_if_true(remove_alleles, r)
            self._set_to_true_if_true(matched_alleles, m)
        if self.prev_clinvar:
            k,m = self.filter_on_existing_clnsig(record)
            self._set_to_true_if_true(matched_alleles, m)
            self._set_to_true_if_true(keep_alleles, k)
        verdict = []
        for i in range(len(remove_alleles)):
            if keep_alleles[i]:
                verdict.append(False)
            elif remove_alleles[i]:
                verdict.append(True)
            elif self.args.filter_known and matched_alleles[i]:
                verdict.append(True)
            elif self.args.filter_novel and not matched_alleles[i]:
                verdict.append(True)
            else:
                verdict.append(False)
        return verdict, remove_csq
 
    def filter_on_af(self, record):
        remove  = [False] * (len(record.ALLELES) -1)
        af = record.parsed_info_fields(fields=['AF'])['AF']
        for i in range(len(remove)):
            if self.args.af:
                if af[i] is not None and af[i] > self.args.af:
                    remove[i] = True
            if self.args.min_af:
                if af[i] is None or af[i] < self.args.min_af:
                    remove[i] = True
        return remove

    def filter_on_existing_cadd_phred(self, record):
        remove  = [False] * (len(record.ALLELES) -1)
        phreds = record.parsed_info_fields(fields=['CADD_PHRED_score'])
        if 'CADD_PHRED_score' in phreds:
            for i in range(len(remove)):
                if (phreds['CADD_PHRED_score'][i] is not None and  
                    phreds['CADD_PHRED_score'][i] < self.args.cadd_phred):
                    remove[i] = True
        return remove

    def filter_on_existing_cadd_raw(self, record):
        remove  = [False] * (len(record.ALLELES) -1)
        raws = record.parsed_info_fields(fields=['CADD_raw_score'])
        if 'CADD_raw_score' in raws:
            for i in range(len(remove)):
                if (raws['CADD_raw_score'][i] is not None and
                    raws['CADD_raw_score'][i] < self.args.cadd_raw):
                    remove[i] = True
        return remove

    def filter_on_existing_freq(self, record):
        remove  = [False] * (len(record.ALLELES) -1)
        matched = [False] * (len(record.ALLELES) -1)
        parsed = record.parsed_info_fields(fields=self.prev_freqs)
        for annot in parsed:
            if parsed[annot] is None:
                continue
            for i in range(len(remove)):
                if parsed[annot][i] is not None:
                    matched[i] = True
                    if self.args.freq:
                        if parsed[annot][i] >= self.args.freq:
                            remove[i] = True
                    if self.args.min_freq:
                        if parsed[annot][i] < self.args.min_freq:
                            remove[i] = True
        return remove,matched

    def filter_on_existing_build(self, record):
        remove  = [False] * (len(record.ALLELES) -1)
        matched = [False] * (len(record.ALLELES) -1)
        parsed = record.parsed_info_fields(fields=self.prev_builds)
        for annot in parsed:
            if parsed[annot] is None:
                continue
            for i in range(len(remove)):
                if parsed[annot][i] is not None:
                    matched[i] = True
                    if self.args.build:
                        if parsed[annot][i] <= self.args.build:
                            remove[i] = True
                    if self.args.min_freq:
                        if parsed[annot][i] > self.args.max_build:
                            remove[i] = True
        return remove,matched

    def filter_on_existing_clnsig(self, record):
        keep    = [False] * (len(record.ALLELES) -1)
        matched = [False] * (len(record.ALLELES) -1)
        parsed = record.parsed_info_fields(fields=self.prev_clinvar)
        for annot in parsed:
            if parsed[annot] is None:
                continue
            for i in range(len(remove)):
                if parsed[annot][i] is not None:
                    matched[i] = True
                    if self.args.clinvar_path:
                        if ([i for i in ['4', '5'] if i in 
                            parsed[annot][i].split('|')]):
                                keep[i] = True
        return keep,matched

    def filter_global(self, record):
        ''' Return True if record fails any global variant filters.'''
        if self.args.pass_filters:
            if record.FILTER != 'PASS':
                return True
        if self.args.variant_quality is not None:
            if record.QUAL < self.args.variant_quality:
                return True
        return False

    def get_cadd_filter(self):
        if self.args.cadd_directory or self.args.cadd_files:
            cadd_args = {'cadd_files': self.args.cadd_files,
                         'cadd_dir': self.args.cadd_directory,
                         'min_phred': self.args.cadd_phred,
                         'min_raw_score': self.args.cadd_raw}
            cf = CaddFilter(**cadd_args)
            for f,d in cf.info_fields.items():
                self.logger.debug("Adding {} annotation")
                self.input.header.add_header_field(name=f, dictionary=d, 
                                          field_type='INFO')
            return cf
        else:
            if self.args.cadd_phred is not None and not self.prev_cadd_phred:
                raise RuntimeError("--cadd_phred cutoff given but VCF does " + 
                                   "not have CADD_PHRED_score INFO field " + 
                                   "in its header and no CADD score files " +
                                   "have been specified with --cadd_files or" + 
                                   " --cadd_dir arguments.")
            if self.args.cadd_raw is not None and not self.prev_cadd_raw:
                raise RuntimeError("--cadd_raw cutoff given but VCF does " + 
                                   "not have CADD_raw_score INFO field " + 
                                   "in its header and no CADD score files " +
                                   "have been specified with --cadd_files " + 
                                   "or --cadd_dir arguments.")
        return None

    def get_vcf_filter_classes(self):
        filters = []
        uni_args = {}
        if self.args.freq is not None:
            uni_args["freq"] = self.args.freq
        if self.args.min_freq is not None:
            uni_args["min_freq"] = self.args.min_freq
        # get dbSNP filter
        for dbsnp in self.args.dbsnp:
            prefix = self.check_info_prefix('VASE_dbSNP')
            kwargs = {"vcf" : dbsnp, "prefix" : prefix, 
                      "clinvar_path" : self.args.clinvar_path,}
            if self.args.build is not None:
                kwargs['build'] = self.args.build
            if self.args.max_build is not None:
                kwargs['max_build'] = self.args.max_build
            kwargs.update(uni_args)
            dbsnp_filter = dbSnpFilter(**kwargs)
            filters.append(dbsnp_filter)
            for f,d in dbsnp_filter.added_info.items():
                self.logger.debug("Adding dbSNP annotation {}" .format(f))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                          field_type='INFO')
        # get gnomAD/ExAC filters
        for gnomad in self.args.gnomad:
            prefix = self.check_info_prefix('VASE_gnomAD')
            kwargs = {"vcf" : gnomad, "prefix" : prefix}
            kwargs.update(uni_args)
            gnomad_filter = GnomadFilter(**kwargs)
            filters.append(gnomad_filter)
            for f,d in gnomad_filter.added_info.items():
                self.logger.debug("Adding gnomAD/ExAC annotation {}" 
                                  .format(f))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                          field_type='INFO')
        #get other VCF filters
        for var_filter in self.args.vcf_filter:
            vcf_and_id = var_filter.split(',')
            if len(vcf_and_id) != 2:
                raise RuntimeError("Expected two comma separated values for " +
                                   "--vcf_filter argument '{}'"
                                   .format(var_filter))
            prefix = self.check_info_prefix('VASE_' + vcf_and_id[1])
            kwargs = {"vcf" : vcf_and_id[0], "prefix" : prefix}
            kwargs.update(uni_args)
            vcf_filter = VcfFilter(**kwargs)
            filters.append(vcf_filter)
            for f,d in vcf_filter.added_info.items():
                self.logger.debug("Adding {} annotation from {}" 
                                  .format(f, vcf_and_id[0]))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                          field_type='INFO')
        return filters

    def _get_prev_annotations(self):
        self.prev_annots = set()
        self.info_prefixes = set()
        for info in self.input.metadata['INFO']:
            match = re.search('^(VASE_\w+)_\w+(_\d+)?', info)
            if match:
                self.prev_annots.add(info)
                self.info_prefixes.add(match.group(1))
                self.logger.debug("Identified previously annotated VASE INFO" +
                                  " field '{}'" .format(info))
        self._parse_prev_vcf_filter_annotations()
        if 'CADD_PHRED_score' in self.input.metadata['INFO']:
            if (self.input.metadata['INFO']['CADD_PHRED_score'][-1]['Number'] == 'A' and
                self.input.metadata['INFO']['CADD_PHRED_score'][-1]['Type'] == 'Float'):
                self.prev_cadd_phred = True
        if 'CADD_raw_score' in self.input.metadata['INFO']:
            if (self.input.metadata['INFO']['CADD_raw_score'][-1]['Number'] == 'A' and
                self.input.metadata['INFO']['CADD_raw_score'][-1]['Type'] == 'Float'):
                self.prev_cadd_raw = True

    def _parse_prev_vcf_filter_annotations(self):
        frq_annots = []
        bld_annots = []
        cln_annots = []
        get_matching = self.args.filter_known or self.args.filter_novel
        if (not self.args.ignore_existing_annotations and 
           (self.args.freq or self.args.min_freq or get_matching)):
            for annot in sorted(self.prev_annots):
                match = re.search('^VASE_dbSNP|gnomAD(_\d+)?_(CAF|AF)(_\w+)?', 
                                  annot)
                if match:
                    if (self.input.metadata['INFO'][annot][-1]['Number'] == 'A' and 
                        self.input.metadata['INFO'][annot][-1]['Type'] == 'Float'): 
                        self.logger.info("Found previous allele frequency " + 
                                          "annotation '{}'".format(annot))
                        frq_annots.append(annot)
        if (not self.args.ignore_existing_annotations and (self.args.build or 
            self.args.max_build or get_matching)):
            for annot in sorted(self.prev_annots):
                match = re.search('^VASE_dbSNP(_\d+)?_dbSNPBuildID', annot)
                if match:
                    if (self.input.metadata['INFO'][annot][-1]['Number'] == 'A' and 
                      self.input.metadata['INFO'][annot][-1]['Type'] == 'Integer'): 
                        self.logger.info("Found previous dbSNP build " + 
                                          "annotation '{}'".format(annot))
                        bld_annots.append(annot)
        if (not self.args.ignore_existing_annotations and 
            (self.args.clinvar_path or get_matching)):
            for annot in sorted(self.prev_annots):
                match = re.search('^VASE_dbSNP(_\d+)?_CLNSIG', annot)
                if match:
                    if (self.input.metadata['INFO'][annot][-1]['Number'] == 'A' and 
                      self.input.metadata['INFO'][annot][-1]['Type'] == 'String'): 
                        self.logger.info("Found previous ClinVar " + 
                                          "annotation '{}'".format(annot))
                        cln_annots.append(annot)
        # if using gnomAD file for burden counting use internal annotations
        # for frequency filtering, if specified
        if (not self.args.ignore_existing_annotations and 
                self.args.gnomad_burden and 
                (self.args.freq or self.args.min_freq)):
            #check only relatively outbred pops by default
            pops = set(("POPMAX", "AFR", "AMR", "EAS", "FIN", "NFE", "SAS"))
            for info in self.input.metadata['INFO']:
                match = re.search(r'''^AF_([A-Z]+)$''', info)
                if match and match.group(1) in pops:
                    if (self.input.metadata['INFO'][info][-1]['Number'] == 'A'
                            and
                            self.input.metadata['INFO'][info][-1]['Type'] == 
                            'Float'):
                        self.logger.info("Found gnomAD allele frequency " + 
                                          "annotation '{}'".format(info))
                        frq_annots.append(info)
        self.prev_freqs = tuple(frq_annots)
        self.prev_builds = tuple(bld_annots)
        self.prev_clinvar = tuple(cln_annots)

    def check_info_prefix(self, name):
        if name in self.info_prefixes:
            self.logger.debug("INFO field {} already exists - trying another"
                              .format(name))
            match = re.search('_(\d+)$', name) 
            if match:
                #already has an appended '_#' - increment and try again
                i = int(match.group(1))
                name = re.sub(match.group(1) + '$', str(i + 1), name)
                return self.check_info_prefix(name)
            else:
                #append _1
                return self.check_info_prefix(name + '_1')
        self.info_prefixes.add(name)
        return name

    def print_header(self):
        ''' 
            Write a VCF header for output that consists of the input VCF
            header data plus program arguments and any new INFO/FORMAT 
            fields.
        '''

        vase_opts = []
        for k,v in vars(self.args).items():
            vase_opts.append('--{} {}'.format(k, v))
        self.input.header.add_header_field(name="vase.py", 
                                   string='"' + str.join(" ", vase_opts) + '"')
        self.out.write(str(self.input.header))

    def get_output(self):
        ''' 
            Return an output filehandle. If no output specified return 
            sys.stdout, else, if output name ends with .gz or .bgz return a 
            bgzf.BgzfWriter object and otherwise return a standard 
            filehandle.
        '''

        if isinstance(self.args.output, str):
            if self.args.output.endswith(('.gz', '.bgz')):
                try:
                    from Bio import bgzf
                except ImportError:
                    raise Exception("Can not import bgzf via " + 
                                    "biopython. Please install biopython in" +
                                    " order to write bgzip compressed " + 
                                    "(.gz/.bgz) output.")
                fh = bgzf.BgzfWriter(self.args.output)
            else:
                fh = open(self.args.output, 'w')
        else:
            fh = sys.stdout
        return fh

    def get_report_filehandles(self):
        fhs = {'recessive' : None,
               'dominant' : None,
               'de_novo' : None,}
        if self.args.report_prefix is not None:
            if self.args.biallelic or self.args.singleton_recessive:
                f = self.args.report_prefix + ".recessive.report.tsv"
                fhs['recessive'] = open(f, 'w')
            if self.args.dominant or self.args.singleton_dominant:
                f = self.args.report_prefix + ".dominant.report.tsv"
                fhs['dominant'] = open(f, 'w')
            if self.args.de_novo:
                f = self.args.report_prefix + ".de_novo.report.tsv"
                fhs['de_novo'] = open(f, 'w')
        return fhs

    def _set_to_true_if_true(self, alist, values):
        for i in range(len(alist)):
            if values[i]:
                alist[i] = True


    def _get_family_filter(self):
        if self.family_filter is not None:
            return self.family_filter
        infer = True
        no_ped = False
        if not self.ped:
            if (not self.args.singleton_recessive and 
                not  self.args.singleton_dominant):
                raise Exception("Inheritance filtering options require a " + 
                                "PED file specified using --ped or else " + 
                                "samples specified using " + 
                                "--singleton_recessive or " +
                                "--singleton_dominant arguments")
            else:
                self.ped = self._make_ped_io()
                no_ped = True
                infer = False
        self.family_filter = FamilyFilter(ped=self.ped, vcf=self.input,
                                          gq=self.args.gq, dp=self.args.dp,
                                          het_ab=self.args.het_ab, 
                                          hom_ab=self.args.hom_ab, 
                                          infer_inheritance=infer,
                                          logging_level=self.logger.level)
        
        for s in self.args.seg_controls: 
            indv = Individual(s, s, 0, 0, 0, 1)
            try:
                self.ped.add_individual(indv)
            except PedError:
                pass
        if not no_ped:
            for s in set(self.args.singleton_recessive + 
                         self.args.singleton_dominant):
                indv = Individual(s, s, 0, 0, 0, 2)
                try:
                    self.ped.add_individual(indv)
                except PedError:
                    raise Exception("Sample '{}' ".format(s) + "specified as" +
                                    " either --singleton_recessive or " +
                                    "--singleton_dominant already exists in" +
                                    " PED file {}" .format(self.ped.filename))
        for s in set(self.args.singleton_dominant):
            self.family_filter.inheritance_patterns[s].append('dominant')
        for s in set(self.args.singleton_recessive):
            self.family_filter.inheritance_patterns[s].append('recessive')
            

    def _make_ped_io(self):
        p_string = ''
        for s in set(self.args.singleton_recessive + 
                     self.args.singleton_dominant):
            p_string += str.join("\t", (s, s, "0", "0", "0", "2")) + "\n" 
        ped = io.StringIO(p_string)
        return PedFile(ped)

    def _set_logger(self):
        self.logger = logging.getLogger("VASE")
        if self.args.debug:
            self.logger.setLevel(logging.DEBUG)
        elif self.args.quiet:
            self.logger.setLevel(logging.WARNING)
        else:
            self.logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
                        '[%(asctime)s] %(name)s - %(levelname)s - %(message)s')
        ch = logging.StreamHandler()
        ch.setLevel(self.logger.level)
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

    def _check_got_inherit_filter(self):
        if self.args.de_novo or self.args.biallelic or self.args.dominant:
            if (not self.recessive_filter and not self.de_novo_filter and
                not self.dominant_filter):
                raise Exception("No inheritance filters could be created " + 
                                "with current settings. Please check your " +
                                "ped/sample inputs or run without --biallelic"+
                                "/--dominant/--de_novo options.")

    def _get_dominant_filter(self):
        self._get_family_filter()
        self._get_control_filter()
        self.dominant_filter = DominantFilter(
                                       self.family_filter, gq=self.args.gq,
                                       dp=self.args.dp, 
                                       het_ab=self.args.het_ab,
                                       hom_ab=self.args.hom_ab,
                                       min_families=self.args.min_families, 
                                       report_file=self.report_fhs['dominant'])
        if not self.dominant_filter.affected:
            msg = ("No samples fit a dominant model - can not use dominant " + 
                   "filtering")
            if not self.args.biallelic and not self.args.de_novo:
                raise Exception("Error: " + msg)
            else:
                self.logger.warn(msg + ". Will continue with other models.")
                self.dominant_filter = None
        else:
            for f,d in self.dominant_filter.get_header_fields().items():
                self.logger.debug("Adding DominantFilter annotation {}" 
                                  .format(f))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                                   field_type='INFO')
                if self.args.min_families > 1:
                    self.use_cache = True

    def _get_de_novo_filter(self):
        self._get_family_filter()
        self._get_control_filter()
        self.de_novo_filter = DeNovoFilter(
                                        self.family_filter, gq=self.args.gq,
                                        dp=self.args.dp, 
                                        het_ab=self.args.het_ab,
                                        hom_ab=self.args.hom_ab,
                                        min_families=self.args.min_families,
                                        report_file=self.report_fhs['de_novo'])
        if not self.de_novo_filter.affected:
            msg = ("No samples fit a de novo model - can not use de novo " + 
                   "filtering")
            if not self.args.biallelic and not self.args.dominant:
                raise Exception("Error: " + msg)
            else:
                self.logger.warn(msg + ". Will continue with other models.")
                self.de_novo_filter = None
        else:
            for f,d in self.de_novo_filter.get_header_fields().items():
                self.logger.debug("Adding DeNovoFilter annotation {}" 
                                  .format(f))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                                   field_type='INFO')
                if self.args.min_families > 1:
                    self.use_cache = True

    def _get_recessive_filter(self):
        self._get_family_filter()
        self.recessive_filter = RecessiveFilter(
                                      self.family_filter, gq=self.args.gq, 
                                      dp=self.args.dp, 
                                      het_ab=self.args.het_ab,
                                      hom_ab=self.args.hom_ab,
                                      min_families=self.args.min_families,
                                      report_file=self.report_fhs['recessive'])
        if not self.recessive_filter.affected:
            msg = ("No samples fit a recessive model - can not use biallelic" + 
                  " filtering")
            if not self.args.de_novo and not self.args.dominant:
                raise Exception("Error: " + msg)
            else:
                self.logger.warn(msg + ". Will continue with other models.")
                self.recessive_filter = None
        else:
            self.use_cache = True
            for f,d in self.recessive_filter.get_header_fields().items():
                self.logger.debug("Adding RecessiveFilter annotation {}" 
                                  .format(f))
                self.input.header.add_header_field(name=f, dictionary=d, 
                                                   field_type='INFO')

    def _get_control_filter(self):
        if self.control_filter:
            return
        self.control_filter = ControlFilter(vcf=self.input, 
                                            family_filter=self.family_filter,
                                            gq=self.args.gq, dp=self.args.dp,
                                            het_ab=self.args.het_ab,
                                            hom_ab=self.args.hom_ab,
                                            n_controls=self.args.n_controls,)

    def _var_or_vars(self, n):
        if n == 1:
            return 'variant'
        return 'variants'


class VariantCache(object):
    ''' 
        Store a collection of CachedVariants that can be outputted
        once certain conditions are met (e.g. once we've checked 
        whether variants match compound heterozygous variation in a 
        gene). Keeps track of VEP Features encountered while adding to
        the cache so that the cache can be released for output once the
        current record is outside of the relevant features.
    '''

    __slots__ = ['cache', 'features', 'output_ready']

    def __init__(self):
        self.cache = []
        self.features = set()
        self.output_ready = []

    def check_record(self, record):
        ''' 
            Check whether features in given record are disjoint with 
            those in cache and if so move variants from cache to 
            output_ready. The given record is NOT added to the cache.
        '''
        these_feats = set([x['Feature'] for x in record.CSQ])
        if self.features and these_feats.isdisjoint(self.features):
            self.add_cache_to_output_ready()
            self.features.clear()

    def add_record(self, record, can_output=False):
        these_feats = set([x['Feature'] for x in record.CSQ])
        if self.features and these_feats.isdisjoint(self.features):
            self.add_cache_to_output_ready()
            self.features = these_feats
        else:
            self.features.update(these_feats)
        self.cache.append(CachedVariant(record, can_output))
        
    def add_cache_to_output_ready(self):
        ''' Adds itemst in cache to output_ready and clears cache.'''
        self.output_ready.extend(self.cache)
        self.cache = []

class CachedVariant(object):
    ''' 
        Store a variant that will or might need outputting later 
        depending on, for example, determining whether they fit a 
        recessive inheritance pattern.
    '''

    __slots__ = ['record', 'can_output', 'var_id']

    def __init__(self, record, can_output=False):
        self.record = record
        self.can_output = can_output
        self.var_id = "{}:{}-{}/{}" .format(record.CHROM, record.POS, 
                                            record.REF, record.ALT)

        
