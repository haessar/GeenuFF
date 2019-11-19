import enum


# Enum makers
def join_to_enum(name, *args):
    """joins enums from args into returned enum"""
    enum_bits = []
    for cls in args:
        enum_bits += list(cls)
    out = enum.Enum(name, [(x.name, x.value) for x in enum_bits])
    return out


def make_enum(name, *args):
    """makes enum from list of strings"""
    return enum.Enum(name, [(x, x) for x in args])


########
# GFF
########

# SuperLocus
CODING_GENE = 'coding_gene'
NON_CODING_GENE = 'non_coding_gene'
PSEUDOGENE = 'pseudogene'
OPERON = 'operon'
GENE = 'gene'

SuperLocus = make_enum('SuperLocus', CODING_GENE, NON_CODING_GENE, PSEUDOGENE, OPERON)
SuperLocusHistorical = make_enum('SuperLocusHistorical', GENE)
SuperLocusAll = join_to_enum('SuperLocusAll', SuperLocus, SuperLocusHistorical)

# Transcript
MRNA = 'mRNA'
TRNA = 'tRNA'
RRNA = 'rRNA'
MIRNA = 'miRNA'
SNORNA = 'snoRNA'
SNRNA = 'snRNA'
SRP_RNA = 'SRP_RNA'
LNC_RNA = 'lnc_RNA'
PRE_MIRNA = 'pre_miRNA'
RNASE_MRP_RNA = 'RNase_MRP_RNA'

TRANSCRIPT = 'transcript'
PRIMARY_TRANSCRIPT = 'primary_transcript'
PSEUDOGENIC_TRANSCRIPT = 'pseudogenic_transcript'

TranscriptLevelNice = make_enum('TranscriptLevelNice', MRNA, TRNA, RRNA, MIRNA, SNORNA, SNRNA, SRP_RNA,
                                LNC_RNA, PRE_MIRNA, RNASE_MRP_RNA)
TranscriptLevelInput = make_enum('TranscriptLevelInput', TRANSCRIPT, PRIMARY_TRANSCRIPT, PSEUDOGENIC_TRANSCRIPT)
TranscriptLevelAll = join_to_enum('TranscriptLevelAll', TranscriptLevelNice, TranscriptLevelInput)

# other features
EXON = 'exon'
FIVE_PRIME_UTR = 'five_prime_UTR'
THREE_PRIME_UTR = 'three_prime_UTR'
CDS = 'CDS'
REGION = 'region'
BIOLOGICAL_REGION = 'biological_region'
CHROMOSOME = 'chromosome'
SUPERCONTIG = 'supercontig'
MATCH = 'match'
CDNA_MATCH = 'cDNA_match'
NCRNA_GENE = 'ncRNA_gene'
NCRNA = 'ncRNA'
SCRNA = 'scRNA'
UNCONFIRMED_TRANSCRIPT = 'unconfirmed_transcript'
SCAFFOLD = 'scaffold'
C_GENE_SEGMENT = 'C_gene_segment'
V_GENE_SEGMENT = 'V_gene_segment'
D_GENE_SEGMENT = 'D_gene_segment'
J_GENE_SEGMENT = 'J_gene_segment'
VAULTRNA_PRIMARY_TRANSCRIPT = 'vaultRNA_primary_transcript'
# ignorable from Augustus
START_CODON = 'start_codon'
STOP_CODON = 'stop_codon'
INTRON = 'intron'
TRANSCRIPTION_START_SITE = 'transcription_start_site'  # transcription_start_site
TRANSCRIPTION_TERMINATION_SITE = 'transcription_end_site'  # transcription_termination_site
TRANSCRIPTION_START_SITE2 = 'tss'  # transcription_start_site (older Augustus version)
TRANSCRIPTION_TERMINATION_SITE2 = 'tts'  # transcription_termination_site
FIVE_PRIME_UTR_LOWER = 'five_prime_utr'
THREE_PRIME_UTR_LOWER = 'three_prime_utr'

IgnorableGFFFeatures = make_enum('IgnorableGFFFeatures', REGION, BIOLOGICAL_REGION, CHROMOSOME,
                                 SUPERCONTIG, MATCH, CDNA_MATCH, NCRNA_GENE, NCRNA, SCRNA,
                                 UNCONFIRMED_TRANSCRIPT, SCAFFOLD, C_GENE_SEGMENT, V_GENE_SEGMENT,
                                 D_GENE_SEGMENT, J_GENE_SEGMENT, VAULTRNA_PRIMARY_TRANSCRIPT,
                                 FIVE_PRIME_UTR, THREE_PRIME_UTR, START_CODON,
                                 STOP_CODON, INTRON, TRANSCRIPTION_START_SITE,
                                 TRANSCRIPTION_TERMINATION_SITE, FIVE_PRIME_UTR_LOWER,
                                 THREE_PRIME_UTR_LOWER, TRANSCRIPTION_START_SITE2,
                                 TRANSCRIPTION_TERMINATION_SITE2)
UsefulGFFSequenceFeatures = make_enum('UsefulGFFSequenceFeatures', EXON, CDS)
UsefulGFFFeatures = join_to_enum('UsefulGFFFeatures', SuperLocusAll, TranscriptLevelAll,
                                 UsefulGFFSequenceFeatures)
AllKnownGFFFeatures = join_to_enum('AllKnownGFFFeatures', IgnorableGFFFeatures, UsefulGFFFeatures)


########
# Geenuff
########

GEENUFF_TRANSCRIPT = 'geenuff_transcript'
GEENUFF_CDS= 'geenuff_cds'
GEENUFF_INTRON = 'geenuff_intron'
GeenuffSequenceFeature = make_enum('GeenuffSequenceFeature', GEENUFF_TRANSCRIPT, GEENUFF_CDS,
                                   GEENUFF_INTRON)

# Geenuff error types
MISSING_UTR_5P = 'missing_utr_5p'
MISSING_UTR_3P = 'missing_utr_3p'
EMPTY_SUPER_LOCUS = 'empty_super_locus'
MISSING_START_CODON = 'missing_start_codon'
MISSING_STOP_CODON = 'missing_stop_codon'
WRONG_PHASE_5P = 'wrong_starting_phase'
MISMATCHED_PHASE_3P = 'mismatched_ending_phase'
OVERLAPPING_EXONS = 'overlapping_exons'
TOO_SHORT_INTRON = 'too_short_intron'
SL_OVERLAP_ERROR = 'super_loci_overlap_error'
MISMATCHING_STRANDS = 'missmatching_strands'
Errors = make_enum('Errors', MISSING_UTR_5P, MISSING_UTR_3P, EMPTY_SUPER_LOCUS, MISSING_START_CODON,
                   MISSING_STOP_CODON, WRONG_PHASE_5P, MISMATCHED_PHASE_3P, OVERLAPPING_EXONS,
                   TOO_SHORT_INTRON, SL_OVERLAP_ERROR, MISMATCHING_STRANDS)

GeenuffFeature = join_to_enum('GeenuffFeature', GeenuffSequenceFeature, Errors)
