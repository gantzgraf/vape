

class PedFile(object):
    ''' 
        A class for parsing and storing information from a PED file.
        A PED file is a white-space (space or tab) delimited file with 
        the first six mandatory columns:

             Family ID
             Individual ID
             Paternal ID
             Maternal ID
             Sex (1=male; 2=female; other=unknown)
             Phenotype

        Affection status should be coded:

            -9 missing 
             0 missing
             1 unaffected
             2 affected

    '''

    def __init__(self, filename):
        
        self.filename = filename
        self.families = {}
        self.individuals = {}
        with open (filename) as ped:
            self._parse_ped(ped)

    def _parse_ped(self, fh):
        for line in fh:
            line = line.rstrip()
            if line.startswith('#'): continue   #skip comments
            if not line: continue   #skip blanks
            cols = line.split()
            if len(cols) < 6:
                raise Exception("Not enough fields for PED file " + 
                                "'{}'.\n" .format(self.filename) + 
                                "Offending line was: {}" .format(line))
            indv = Individual(*cols[:6])
            if indv.iid in self.individuals:
                raise PedError("Duplicate individual ID '{}'".format(indv.iid)+
                               "in PED file '{}'. " . format(self.filename) + 
                               "Please ensure all individual IDs are unique.")
            self.individuals[indv.iid] = indv
            if indv.fid in self.families:
                self.families[indv.fid].add_individual(indv)
            else:
                fam = Family(indv.fid, [indv])
                self.families[indv.fid] = fam

    def get_affected(self):
        return (i for i in self.individuals 
                if self.individuals[i].is_affected())

    def get_unaffected(self):
        return (i for i in self.individuals 
                if self.individuals[i].is_unaffected())

    def get_males(self):
        return (i for i in self.individuals 
                if self.individuals[i].is_male())

    def get_females(self):
        return (i for i in self.individuals 
                if self.individuals[i].is_female())


class Family(PedFile):
    ''' Stores a single family as defined in a PED file '''

    def __init__(self, fid, individuals=None):
        self.fid = fid
        self.individuals = {}
        self.parents = {}
        self.founder = None
        if individuals is not None:
            for i in individuals:
                self.add_individual(i)

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self.individuals
        elif isinstance(key, Individual):
            return key.iid in self.individuals
        else:
            return False

    def add_individual(self, individual):
        ''' 
            Add an Individual object to Family. The family ID (fid) of 
            the added individual will be changed to that of the Family 
            object if it differs.
        '''
        if individual.iid in self.individuals:
            raise PedError("Duplicate individual '{}'" .format(individual.iid)+
                           " added to family '{}'." .format(self.fid))
        individual.fid = self.fid
        for i in self.individuals.values():
            if (i.mother != '0' and i.mother == individual.mother and 
                i.father != '0' and i.father == individual.father):
                i.siblings.append(individual.iid)
                individual.siblings.append(i.iid)
            elif ( (i.mother != '0' and i.mother == individual.mother) or
                  (i.father != '0' and i.father == individual.father)):
                i.half_siblings.append(individual.iid)
                individual.half_siblings.append(i.iid)
        for parent in [individual.mother, individual.father]:
            if parent != '0':
                if parent not in self.parents:
                    self.parents[parent] = [individual.iid]
                else:
                    self.parents[parent].append(individual.iid)
                if parent in self.individuals:
                    self.individuals[parent].children = self.parents[parent]
        if individual.iid in self.parents:
            individual.children = self.parents[individual.iid]
        self.individuals[individual.iid] = individual

    def set_founder(self, founder):
        if founder not in self.iids:
            raise PedError("Can not set founder to '{}' " .format(founder) + 
                           "for family '{}' - ".format(self.fid) + 
                           "no individual with matching ID found in family")
        if isinstance(founder, str):
            self.founder = founder
        elif isinstance(founder, Individual):
            self.founder = founder.iid


class Individual(object):
    ''' Stores information about a single individual in a PED file '''

    def __init__(self, fid, iid, father, mother, sex, phenotype, siblings=[],
                 half_siblings=[], children=[]):
        self.fid = fid
        self.iid = iid
        self.father = father
        self.mother = mother
        try:
            self.sex = int(sex)
        except ValueError: #any value other than 1 or 2 = unknown gender
            self.sex = 0
        self.phenotype = int(phenotype)
        self.siblings = []
        self.half_siblings = []
        self.children = []
        if siblings:
            self.siblings.append(siblings)
        if half_siblings:
            self.half_siblings.append(half_siblings)
        if children:
            self.children.append(children)

    def is_affected(self):
        return self.phenotype == 2

    def is_unaffected(self):
        return self.phenotype == 1

    def is_unknown_phenotype(self):
        return self.phenotype != 1 and self.phenotype != 2

    def is_male(self):
        return self.sex == 1

    def is_female(self):
        return self.sex == 2

    def is_unknown_gender(self):
        return self.sex != 1 and self.sex != 2

class PedError(Exception):
    pass
