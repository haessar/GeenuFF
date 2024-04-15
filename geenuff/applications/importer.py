import os
import math
import logging
import shutil
import sys

import intervaltree
from pprint import pprint  # for debugging
from abc import ABC, abstractmethod
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dustdas import gffhelper, fastahelper
from .. import orm
from .. import types
from .. import helpers
from ..base.helpers import (get_strand_direction, get_geenuff_start_end, has_start_codon,
                            has_stop_codon, in_enum_values)


class GFFValidityError(Exception):
    pass


# core queue prep
class InsertionQueue(helpers.QueueController):
    def __init__(self, session, engine):
        super().__init__(session, engine)
        self.super_locus = helpers.CoreQueue(orm.SuperLocus.__table__.insert())
        self.transcript = helpers.CoreQueue(orm.Transcript.__table__.insert())
        self.transcript_piece = helpers.CoreQueue(orm.TranscriptPiece.__table__.insert())
        self.protein = helpers.CoreQueue(orm.Protein.__table__.insert())
        self.feature = helpers.CoreQueue(orm.Feature.__table__.insert())
        self.association_transcript_piece_to_feature = helpers.CoreQueue(
            orm.association_transcript_piece_to_feature.insert())
        self.association_protein_to_feature = helpers.CoreQueue(
            orm.association_protein_to_feature.insert())
        self.association_transcript_to_protein = helpers.CoreQueue(
            orm.association_transcript_to_protein.insert())

        self.ordered_queues = [
            self.super_locus, self.transcript, self.transcript_piece, self.protein, self.feature,
            self.association_transcript_piece_to_feature, self.association_protein_to_feature,
            self.association_transcript_to_protein
        ]


class InsertCounterHolder(object):
    """provides incrementing unique integers to be used as primary keys for bulk inserts"""
    feature = helpers.Counter(orm.Feature)
    protein = helpers.Counter(orm.Protein)
    transcript = helpers.Counter(orm.Transcript)
    super_locus = helpers.Counter(orm.SuperLocus)
    transcript_piece = helpers.Counter(orm.TranscriptPiece)
    genome = helpers.Counter(orm.Genome)


class OrganizedGeenuffImporterGroup(object):
    """Stores the handler objects for a super locus in an organized fashion.
    The format is similar to the one of OrganizedGFFEntryGroup, but it stores objects
    according to the Geenuff way of saving genomic annotations. This format can then
    be checked for errors and changed accordingly before being inserted into the db.

    The importers are organized in the following way:

    importers = {
        'super_locus' = super_locus_importer,
        'transcripts': [
            {
                'transcript': transcript_importer,
                'transcript_piece: transcript_piece_importer,
                'transcript_feature': transcript_importer,
                'protein': protein_importer,
                'cds': cds_importer,
                'introns': [intron_importer1, intron_importer2, ..],
                'errors': []  # errors are filled in later
            },
            ...
        ],
    }
    """

    def __init__(self, organized_gff_entries, coord, controller):
        self.coord = coord
        self.controller = controller
        self.importers = {'transcripts': []}
        try:
            self._parse_gff_entries(organized_gff_entries)
        except Exception as e:
            print('Error originally raised while parsing the following entries\n', organized_gff_entries,
                  file=sys.stderr)
            raise e

    def _parse_gff_entries(self, entries):
        """Changes the GFF format into the GeenuFF format. Does all the parsing."""
        sl = entries['super_locus']

        sl_is_plus_strand = get_strand_direction(sl)

        sl_start, sl_end = get_geenuff_start_end(sl.start, sl.end, sl_is_plus_strand)
        sl_i = self.importers['super_locus'] = SuperLocusImporter(entry_type=sl.type,
                                                                  given_name=sl.get_ID(),
                                                                  coord=self.coord,
                                                                  is_plus_strand=sl_is_plus_strand,
                                                                  start=sl_start,
                                                                  end=sl_end,
                                                                  controller=self.controller)
        for t, t_entries in entries['transcripts'].items():
            t_importers = {'errors': []}
            # check for multi inheritance and throw NotImplementedError if found
            if t.get_Parent() is None:
                raise GFFValidityError(f"transcript level feature without Parent found {t}, attributes: {t.attributes}")
            if len(t.get_Parent()) > 1:
                raise NotImplementedError
            t_id = t.get_ID()
            t_is_plus_strand = get_strand_direction(t)
            # create transcript handler
            t_i = TranscriptImporter(entry_type=t.type,
                                     given_name=t_id,
                                     super_locus_id=sl_i.id,
                                     controller=self.controller)
            # create transcript piece handler
            tp_i = TranscriptPieceImporter(given_name=t_id,
                                           transcript_id=t_i.id,
                                           position=0,
                                           controller=self.controller)
            # create transcript feature handler
            tf_i = FeatureImporter(self.coord,
                                   t_is_plus_strand,
                                   types.GEENUFF_TRANSCRIPT,
                                   given_name=t_id,
                                   score=t.score,
                                   source=t.source,
                                   controller=self.controller)
            tf_i.set_start_end_from_gff(t.start, t.end)

            # insert everything so far into the dict
            t_importers['transcript'] = t_i
            t_importers['transcript_piece'] = tp_i
            t_importers['transcript_feature'] = tf_i

            # if it is not a non-coding gene or something like that
            if t_entries['cds']:
                assert all([t_is_plus_strand == get_strand_direction(x) for x in t_entries['cds']])
                # create protein handler
                protein_id = self._get_protein_id_from_cds_list(t_entries['cds'])
                p_i = ProteinImporter(given_name=protein_id,
                                      super_locus_id=sl_i.id,
                                      controller=self.controller)
                # create coding features from exon limits
                if t_is_plus_strand:
                    phase_5p = t_entries['cds'][0].phase
                    phase_3p = t_entries['cds'][-1].phase
                else:
                    phase_5p = t_entries['cds'][-1].phase
                    phase_3p = t_entries['cds'][0].phase
                cds_i = FeatureImporter(self.coord,
                                        t_is_plus_strand,
                                        types.GEENUFF_CDS,
                                        phase_5p=phase_5p,
                                        phase_3p=phase_3p,
                                        score=t.score,
                                        source=t.source,
                                        controller=self.controller)
                # the next two lines are normally enough to define cds start & end
                gff_cds_start = t_entries['cds'][0].start
                gff_cds_end = t_entries['cds'][-1].end
                # however, we have to handle partial gene models that can end in / have hanging introns
                # for a hanging intron the 'exon start' doesn't line up with the transcript start (same for ends)
                gff_exon_start = t_entries['exons'][0].start
                gff_exon_end = t_entries['exons'][-1].end
                if gff_exon_start == gff_cds_start != t.start:
                    # hanging intron at start, CDS feature will be extended to end of transcript
                    # this will be wrong if the start codon exactly aligned w/ exon start and the final exon is
                    # non-coding, but that is rare, and this implementation is more cautious / conservative
                    # (AKA: will create more error masks to reflect the ambiguity)
                    gff_cds_start = t.start
                if gff_exon_end == gff_cds_end != t.end:
                    # hanging intron at end
                    gff_cds_end = t.end

                cds_i.set_start_end_from_gff(gff_cds_start, gff_cds_end)

                # insert everything so far into the dict
                t_importers['protein'] = p_i
                t_importers['cds'] = cds_i

                # create all the introns by taking the transcript, and subtracting the exons
                # (this handles literal sequence-edge cases more reliably than the gaps between exons)
                # what remains is introns and gets FeatureImport setup and inserted into the previously created list
                introns = []
                itree = intervaltree.IntervalTree()
                etree = intervaltree.IntervalTree()
                # bc interval tree only operates w/ start < end
                inv_t_start, inv_t_end = sorted([tf_i.start, tf_i.end])
                itree[inv_t_start:inv_t_end] = t_is_plus_strand
                exons = t_entries['exons']
                for exon in exons:
                    # something really weird is going on here, marking the whole gene as erroneous
                    e_is_plus_strand = get_strand_direction(exon)
                    if e_is_plus_strand != t_is_plus_strand:
                        sl_i.fully_erroneous = True
                        break
                    e_start, e_end = get_geenuff_start_end(exon.start, exon.end, e_is_plus_strand)
                    inv_e_start, inv_e_end = sorted([e_start, e_end])
                    itree.chop(inv_e_start, inv_e_end)
                    # also check for and mark overlapping exons (for now with a backward 'intron' # todo refactor)
                    overlapping = etree[inv_e_start:inv_e_end]
                    if overlapping:
                        overlapper_begin = min([o.begin for o in overlapping])
                        overlapper_end = max([o.end for o in overlapping])
                        if len(overlapping) != 1:
                            logging.warning('handling overlaps of >1 exon... (masking as if unioned), but this is a '
                                            'weird enough sort of error that you should really check what is going on '
                                            'if you read this (around {} {}-{})'.format(self.coord, overlapper_begin,
                                                                                        overlapper_end))

                        ovlp_end = max(overlapper_begin, inv_e_start)
                        ovlp_start = min(overlapper_end, inv_e_end)
                        print('found an overlap {}-{}'.format(ovlp_end, ovlp_start))
                        if not e_is_plus_strand:
                            ovlp_start, ovlp_end = ovlp_end, ovlp_start
                        # insert a dummy 'backwards' intron, which will later be turned into an overlap error
                        intron_err = FeatureImporter(self.coord,
                                                     is_plus_strand=e_is_plus_strand,
                                                     feature_type=types.GEENUFF_INTRON,
                                                     start=ovlp_start,
                                                     end=ovlp_end,
                                                     score=t.score,
                                                     source=t.source,
                                                     controller=self.controller)
                        introns.append(intron_err)
                    etree[inv_e_start:inv_e_end] = e_is_plus_strand

                for interval in itree:
                    i_start, i_end = interval.begin, interval.end
                    if not interval.data:  # if minus strand (data = is_plus_strand)
                        i_start, i_end = i_end, i_start
                    intron_i = FeatureImporter(self.coord,
                                               is_plus_strand=interval.data,
                                               feature_type=types.GEENUFF_INTRON,
                                               start=i_start,
                                               end=i_end,
                                               score=t.score,
                                               source=t.source,
                                               controller=self.controller)
                    introns.append(intron_i)

                introns = sorted(introns, key=lambda x: x.start)
                if t_is_plus_strand:
                    t_importers['introns'] = introns
                else:
                    t_importers['introns'] = introns[::-1]
            self.importers['transcripts'].append(t_importers)
        self._set_longest_transript()

    def _set_longest_transript(self):
        """Looks for the transcript with the longest exon length (cds_length - sum(intron_lengths))
        and sets the 'longest' parameters in all the TranscriptImporters. In case of a tie, the
        first transcript found will be set as longest."""
        def filter_coding_introns(cds_i, introns):
            """Filters the non-coding introns out of introns"""
            cds_range = sorted([cds_i.start, cds_i.end])
            coding_introns = []
            for intron in introns:
                intron_range = sorted([intron.start, intron.end])
                if min(cds_range[1], intron_range[1]) - max(cds_range[0], intron_range[0]) > 0:
                    coding_introns.append(intron)
            return coding_introns

        max_exon_len = -1
        longest_importer = None
        for t in self.importers['transcripts']:
            if 'cds' in t:
                cds_len = abs(t['cds'].start - t['cds'].end)
                coding_introns = filter_coding_introns(t['cds'], t['introns'])
                intron_lengths = sum([abs(i.start - i.end) for i in coding_introns])
                exon_len = cds_len - intron_lengths
                if exon_len > max_exon_len:
                    max_exon_len = exon_len
                    longest_importer = t['transcript']
        for t in self.importers['transcripts']:
            if t['transcript'] is longest_importer:
                t['transcript'].longest = True
            else:
                t['transcript'].longest = False

    @staticmethod
    def _get_protein_id_from_cds_entry(cds_entry):
        # check if anything is labeled as protein_id
        protein_id = cds_entry.attrib_filter(tag='protein_id')
        # failing that, try and get parent ID (presumably transcript, maybe gene)
        if not protein_id:
            protein_id = cds_entry.get_Parent()
        # hopefully take single hit
        if len(protein_id) == 1:
            protein_id = protein_id[0]
            if isinstance(protein_id, gffhelper.GFFAttribute):
                protein_id = protein_id.value
                assert len(protein_id) == 1
                protein_id = protein_id[0]
        # or handle other cases
        elif len(protein_id) == 0:
            protein_id = None
        else:
            raise ValueError('indeterminate single protein id {}'.format(protein_id))
        return protein_id

    @staticmethod
    def _get_protein_id_from_cds_list(cds_entry_list):
        """Returns the protein id of a list of cds gff entries. If multiple ids or no id at all
        are found, an error is raised."""
        protein_ids = set()
        for cds_entry in cds_entry_list:
            protein_id = OrganizedGeenuffImporterGroup._get_protein_id_from_cds_entry(cds_entry)
            if protein_id:
                protein_ids.add(protein_id)
        if len(protein_ids) != 1:
            logging.warning('No protein_id or more than one protein_ids for one transcript')
            # raise ValueError('No protein_id or more than one protein_ids for one transcript')
        return protein_ids.pop()


class OrganizedGFFEntryGroup(object):
    """Takes an entry group (all entries of one super locus) and stores the entries
    in an orderly fashion. Can then return a corresponding OrganizedGeenuffImporterGroup.
    Does not perform error checking, which happens later.

    The entries are organized in the following way:

    entries = {
        'super_locus' = super_locus_entry,
        'transcripts' = {
            transcript_entry1: {
                'exons': [ordered_exon_entry1, ordered_exon_entry2, ..],
                'cds': [ordered_cds_entry1, ordered_cds_entry2, ..]
            },
            transcript_entry2: {
                'exons': [ordered_exon_entry1, ordered_exon_entry2, ..],
                'cds': [ordered_cds_entry1, ordered_cds_entry2, ..]
            },
            ...
        }
    }
    """

    def __init__(self, gff_entry_group, fasta_importer, controller):
        self.fasta_importer = fasta_importer
        self.controller = controller
        self.entries = {'transcripts': {}}
        self.coord = None
        self.add_gff_entry_group(gff_entry_group)

    def add_gff_entry_group(self, entries):
        latest_transcript = None
        for entry in list(entries):
            if in_enum_values(entry.type, types.SuperLocusAll):
                assert 'super_locus' not in self.entries
                self.entries['super_locus'] = entry
            elif in_enum_values(entry.type, types.TranscriptLevel):
                self.entries['transcripts'][entry] = {'exons': [], 'cds': []}
                latest_transcript = entry
            elif latest_transcript is not None:
                if in_enum_values(entry.type, types.ExonLevel):
                    self.entries['transcripts'][latest_transcript]['exons'].append(entry)
                elif in_enum_values(entry.type, types.CDSLevel):
                    self.entries['transcripts'][latest_transcript]['cds'].append(entry)
                else:
                    logging.warning(f'Found unexpected entry type: {entry.type}')
            else:
                logging.warning(f'Ignoring {entry.type} without transcript found in {entry.seqid}: {entries[0].attribute}')

        # set the coordinate
        self.coord = self.fasta_importer.gffid_to_coords[self.entries['super_locus'].seqid]

        # order exon and cds lists by start value (disregard strand for now)
        for _, value_dict in self.entries['transcripts'].items():
            for key in ['exons', 'cds']:
                value_dict[key].sort(key=lambda e: e.start)

    def get_geenuff_importers(self):
        geenuff_importer_group = OrganizedGeenuffImporterGroup(self.entries, self.coord,
                                                               self.controller)
        return geenuff_importer_group.importers


class OrganizedGFFEntries(object):
    """Structures the gff entries coming from gffhelper by seqid and gene. Also does some
    basic gff value cleanup.
    The entries are organized in the following way:

    organized_entries = {
        'seqid1': [
            [gff_entry1_gene1, gff_entry2_gene1, ..],
            [gff_entry1_gene2, gff_entry2_gene2, ..],
        ],
        'seqid2': [
            [gff_entry1_gene1, gff_entry2_gene1, ..],
            [gff_entry1_gene2, gff_entry2_gene2, ..],
        ],
        ...
    }
    """

    def __init__(self, gff_file):
        self.gff_file = gff_file
        self.organized_entries = {}

    def load_organized_entries(self):
        self.organized_entries = {}
        gene_level = [x.value for x in types.SuperLocusAll]

        reader = self._useful_gff_entries()
        first = next(reader, None)

        if first is not None:
            seqid = first.seqid
            gene_group = [first]
            self.organized_entries[seqid] = []
            for entry in reader:
                if entry.type in gene_level:
                    self.organized_entries[seqid].append(gene_group)
                    gene_group = [entry]
                    if entry.seqid != seqid:
                        self.organized_entries[entry.seqid] = []
                        seqid = entry.seqid
                else:
                    gene_group.append(entry)
            self.organized_entries[seqid].append(gene_group)

    def _useful_gff_entries(self):
        skipable = [x.value for x in types.IgnorableGFFFeatures]
        reader = self._gff_gen()
        for entry in reader:
            if entry.type not in skipable:
                yield entry

    def _gff_gen(self):
        known = [x.value for x in types.AllKnownGFFFeatures]
        reader = gffhelper.read_gff_file(self.gff_file)
        for entry in reader:
            if entry.type not in known:
                raise ValueError("unrecognized feature type from gff: {}".format(entry.type))
            else:
                self._clean_entry(entry)
                yield entry

    @staticmethod
    def _clean_entry(entry):
        # always present and integers
        entry.start = int(entry.start)
        entry.end = int(entry.end)
        # clean up score
        if entry.score == '.':
            entry.score = None
        else:
            entry.score = float(entry.score)

        # clean up phase
        if entry.phase == '.':
            entry.phase = None
        else:
            entry.phase = int(entry.phase)
        assert entry.phase in [None, 0, 1, 2]

        # clean up strand
        if entry.strand == '.':
            entry.strand = None
        else:
            assert entry.strand in ['+', '-']


class GFFErrorHandling(object):
    """Deals with error detection and handling of the input features. Does the handling
    in the space of GeenuFF importers.
    Assumes all super locus handler groups to be ordered 5p to 3p and of one strand.
    Works with a list of OrganizedGeenuffImporterGroup, which correspond to a list of
    super loci, and looks for errors. Error features may be inserted and importers be
    removed when deemed necessary.
    """

    def __init__(self, geenuff_importer_groups, controller):
        self.groups = geenuff_importer_groups
        if self.groups:
            self.is_plus_strand = self.groups[0]['super_locus'].is_plus_strand
            self.coord = self.groups[0]['super_locus'].coord
            # make sure self.groups is sorted correctly
            self.groups.sort(key=lambda g: g['super_locus'].start, reverse=not self.is_plus_strand)
        self.controller = controller

    def _sl_neighbor_status(self, slg_prev, slg):
        """checks whether two neighboring super loci overlap and if yes under which conditions.
        Returns one of the following values depending on the relationship:

        'normal': no overlap in any way
        'overlap': the sl overlap but without errors on both sides
        'overlap_error_3p': the sl 3p of the overlap has an 5p utr error
        'overlap_error_5p': the sl 5p of the overlap has an 3p utr error
        'overlap_error_both': both sl have an utr error at the overlap
        'nested': sl is a fully nested gene inside sl_prev
        'nested_error_3p': the nested gene has an 3p utr error
        'nested_error_5p': the nested gene has an 5p utr error
        'nested_error_both': the nested gene has utr errors on both ends
        """
        def _overlap_error_status(slg_prev, slg):
            status = 'overlap'
            for error in self._get_all_errors_of_slg(slg_prev):
                if error.feature_type == types.MISSING_UTR_3P:
                    # a 3p utr error of the previous sl is 5p of the overlap
                    status = 'overlap_error_5p'
                    break
            for error in self._get_all_errors_of_slg(slg):
                if error.feature_type == types.MISSING_UTR_5P:
                    if status == 'overlap':
                        status = 'overlap_error_3p'
                    else:
                        status = 'overlap_error_both'
                    break
            return status

        def _nested_error_status(slg):
            error_types = [e.feature_type for e in self._get_all_errors_of_slg(slg)]
            if types.MISSING_UTR_3P in error_types and types.MISSING_UTR_5P in error_types:
                return 'nested_error_both'
            if types.MISSING_UTR_3P in error_types:
                return 'nested_error_3p'
            if types.MISSING_UTR_5P in error_types:
                return 'nested_error_5p'
            return 'nested'

        sl_prev = slg_prev['super_locus']
        sl = slg['super_locus']
        if self.is_plus_strand and sl_prev.end > sl.start:
            if sl_prev.end > sl.end:
                nested_status = _nested_error_status(slg)
                return nested_status
            else:
                overlap_status = _overlap_error_status(slg_prev, slg)
                return overlap_status
        elif not self.is_plus_strand and sl_prev.end < sl.start:
            if sl_prev.end < sl.end:
                nested_status = _nested_error_status(slg)
                return nested_status
            else:
                overlap_status = _overlap_error_status(slg_prev, slg)
                return overlap_status
        return 'normal'

    def _3p_cds_start(self, transcript):
        """returns the start of the 3p most cds feature"""
        cds = transcript['cds']
        start = cds.start
        # introns are ordered by coordinate with no respect to strand
        intron_ends = [x.end for x in transcript["introns"]]
        if self.is_plus_strand:
            i_ends_within = [i for i in intron_ends if cds.start < i < cds.end]
            if i_ends_within:
                start = max(i_ends_within)
        else:
            i_ends_within = [i for i in intron_ends if cds.end < i < cds.start]
            if i_ends_within:
                start = min(i_ends_within)
        return start

    def _get_all_errors_of_slg(self, slg):
        error_lists = [t['errors'] for t in slg['transcripts']]
        merged_errors = [e for el in error_lists for e in el]
        return merged_errors

    def _print_overlap_error_msg(self, sl, sl_prev, status):
        if status.startswith('overlap'):
            msg = 'overlapping super loci: {} and {} (not nested), type: {}'
        elif status.startswith('nested'):
            msg = 'nested super loci: {} inside {}, type: {}'
        else:
            logging.error('overlapping status is not a known error')
        logging.info(msg.format(sl_prev.given_name, sl.given_name, status))

    def resolve_errors(self):
        for i, group in enumerate(self.groups):
            # the case of a super locus being masked as fully erroneous (for some special reason)
            # mask everything and append error to first transcript
            if group['super_locus'].fully_erroneous:
                sl_i = group['super_locus']
                self._add_error(i, group['transcripts'][0], sl_i.start, sl_i.end, sl_i.is_plus_strand,
                                types.MISMATCHING_STRANDS)
                continue

            # the case of no transcript for a super locus
            if not group['transcripts']:
                logging.error('{} is a gene without any transcripts. This will not be masked.'.format(
                                  group['super_locus'].given_name))
            # other cases
            for transcript in group['transcripts']:
                # if coding transcript
                if 'cds' in transcript:
                    cds = transcript['cds']
                    introns = transcript['introns']
                    tf = transcript['transcript_feature']

                    # the case of missing of implicit UTR ranges
                    # the solution is similar to the one above
                    if cds.start == tf.start:
                        self._add_overlapping_error(i, transcript, cds, '5p', types.MISSING_UTR_5P,
                                                    mark_other_handlers=[tf])
                    if cds.end == tf.end:
                        self._add_overlapping_error(i, transcript, cds, '3p', types.MISSING_UTR_3P,
                                                    mark_other_handlers=[tf])

                    # the case of missing start/stop codon
                    if not has_start_codon(cds.coord.sequence, cds.start, self.is_plus_strand):
                        self._add_overlapping_error(i, transcript, cds, '5p', types.MISSING_START_CODON)
                    if not has_stop_codon(cds.coord.sequence, cds.end, self.is_plus_strand):
                        self._add_overlapping_error(i, transcript, cds, '3p', types.MISSING_STOP_CODON)

                    # the case of wrong 5p phase
                    if cds.phase_5p != 0:
                        self._add_overlapping_error(i, transcript, cds, '5p', types.WRONG_PHASE_5P)

                    # check the case of overlapping sl together with an error in this transcript
                    if i > 0:
                        group_prev = self.groups[i - 1]
                        sl, sl_prev = group['super_locus'], group_prev['super_locus']
                        status = self._sl_neighbor_status(group_prev, group)
                        if status != 'normal':
                            if status == 'overlap_error_5p' or status == 'overlap_error_both':
                                self._add_overlapping_error(i, transcript, sl_prev, '3p',
                                                            types.SL_OVERLAP_ERROR,
                                                            find_next_non_overlapping=True)
                            elif status == 'overlap_error_3p' or status == 'overlap_error_both':
                                self._add_overlapping_error(i, transcript, sl, '5p',
                                                            types.SL_OVERLAP_ERROR,
                                                            find_next_non_overlapping=True)
                            elif status.startswith('nested_error'):
                                # remove all missing 3p/5p errors as they have to be wrong and are
                                # not needed further down the road
                                # we could try to add error halfway to the intron border but
                                # 1. it is not guaranteed that there is such an intron
                                # 2. there could be multiple ones in different transcripts
                                # due to that complexity we are content with adding no errors
                                transcript['errors'] = [e for e in transcript['errors']
                                                        if e.feature_type not in [
                                                             types.MISSING_UTR_3P, types.MISSING_UTR_5P
                                                        ]]
                            self._print_overlap_error_msg(sl_prev, sl, status)

                    if introns:
                        # the case of wrong 3p phase
                        len_3p_exon = abs(cds.end - self._3p_cds_start(transcript))
                        if cds.phase_3p != len_3p_exon % 3:
                            self._add_overlapping_error(i, transcript, cds, '3p',
                                                        types.MISMATCHED_PHASE_3P)

                    faulty_introns = []
                    for j, intron in enumerate(introns):
                        # the case of overlapping exons
                        if ((tf.is_plus_strand and intron.end < intron.start)
                                or (not self.is_plus_strand and intron.end > intron.start)):
                            # mark the overlapping cds regions as errors
                            if j > 0:
                                error_start = introns[j - 1].end
                            else:
                                error_start = tf.start
                            if j < len(introns) - 1:
                                error_end = introns[j + 1].start
                            else:
                                error_end = tf.end
                            self._add_error(i, transcript, error_start, error_end,
                                            self.is_plus_strand, types.OVERLAPPING_EXONS)
                            faulty_introns.append(intron)
                        # the case of a too short intron
                        # todo put the minimum length in a config somewhere
                        elif abs(intron.end - intron.start) < self.controller.config['min_intron_length']:
                            self._add_error(i, transcript, intron.start, intron.end,
                                            self.is_plus_strand, types.TOO_SHORT_INTRON)
                    # do not save faulty introns, the error should be descriptive enough
                    for intron in faulty_introns:
                        introns.remove(intron)

                    # finally, introns can be partial (although this normally happens at a sequence end)
                    for intron in transcript['introns']:
                        if intron.start == tf.start:
                            self._add_overlapping_error(i, transcript, intron, '5p', types.TRUNCATED_INTRON)
                        if intron.end == tf.end:
                            self._add_overlapping_error(i, transcript, intron, '3p', types.TRUNCATED_INTRON)
        # remove all errors that are in the wrong order (caused by overlapping super loci)
        # these can only be removed now as they were needed for further processing
        self._remove_backwards_errors()

    def _remove_backwards_errors(self):
        def is_backwards(e):
            return ((self.is_plus_strand and e.end < e.start)
                    or (not self.is_plus_strand and e.end > e.start))

        for group in self.groups:
            n_removed = 0
            for transcript in group['transcripts']:
                full_len = len(transcript['errors'])
                transcript['errors'] = [e for e in transcript['errors'] if not is_backwards(e)]
                n_removed += full_len - len(transcript['errors'])
            if n_removed > 0:
                msg = ('removed {count} backwards error(s) from overlapping super loci: '
                       'seqid: {seqid}, {geneid}').format(count=n_removed,
                                                          seqid=self.coord.seqid,
                                                          geneid=group['super_locus'].given_name)
                logging.info(msg)

    def _add_error(self, i, transcript_g, start, end, is_plus_strand, error_type):
        error_i = FeatureImporter(self.coord,
                                  is_plus_strand,
                                  error_type,
                                  start=start,
                                  end=end,
                                  controller=self.controller)
        transcript_g['errors'].append(error_i)
        # error msg
        if is_plus_strand:
            strand_str = 'plus'
        else:
            strand_str = 'minus'
        msg = ('marked as erroneous: seqid: {seqid}, {start}--{end}:{geneid}, on {strand} strand, '
               'with type: {type}').format(seqid=self.coord.seqid,
                                           start=start,
                                           end=end,
                                           geneid=self.groups[i]['super_locus'].given_name,
                                           strand=strand_str,
                                           type=error_type)
        logging.warning(msg)

    def _add_overlapping_error(self, i, transcript_g, handler, direction, error_type,
                               find_next_non_overlapping=False, mark_other_handlers=None):
        """Constructs an error features that overlaps halfway to the next super locus
        in the given direction from the given handler if possible. Otherwise mark until the end.
        If the direction is 'whole', the handler parameter is ignored.

        Also sets handler.start_is_biological_start=False (or the end) if necessary
        """
        if mark_other_handlers is None:
            mark_other_handlers = []

        assert direction in ['5p', '3p', 'whole']
        coord = self.groups[i]['super_locus'].coord

        j = i  # do not change i as we need it later
        # set correct upstream error starting point
        if direction in ['5p', 'whole']:
            if find_next_non_overlapping:
                while (j > 0
                          and self._sl_neighbor_status(self.groups[j - 1], self.groups[j]) != 'normal'):
                    j -= 1
            # perform marking
            if j > 0:
                anchor_5p = self._error_border_mark(self.groups[j - 1]['super_locus'],
                                                    self.groups[j]['super_locus'])
            else:
                if self.is_plus_strand:
                    anchor_5p = 0
                else:
                    anchor_5p = coord.length

        # set correct downstream error end point
        if direction in ['3p', 'whole']:
            if find_next_non_overlapping:
                while (j < len(self.groups) - 1
                          and self._sl_neighbor_status(self.groups[j], self.groups[j + 1]) != 'normal'):
                    j += 1
            if j < len(self.groups) - 1:
                anchor_3p = self._error_border_mark(self.groups[j]['super_locus'],
                                                    self.groups[j + 1]['super_locus'])
            else:
                if self.is_plus_strand:
                    anchor_3p = coord.length
                else:
                    anchor_3p = -1

        if direction == '5p':
            error_5p = anchor_5p
            error_3p = handler.start
            for h in [handler] + mark_other_handlers:
                if isinstance(h, FeatureImporter):
                    h.start_is_biological_start = False

        elif direction == '3p':
            error_5p = handler.end
            error_3p = anchor_3p
            for h in [handler] + mark_other_handlers:
                if isinstance(h, FeatureImporter):
                    h.end_is_biological_end = False

        elif direction == 'whole':
            error_5p = anchor_5p
            error_3p = anchor_3p

        if not self._zero_len_coords_at_sequence_edge(error_5p, error_3p, direction, coord):
            self._add_error(i, transcript_g, error_5p, error_3p, self.is_plus_strand, error_type)

    def _zero_len_coords_at_sequence_edge(self, error_5p, error_3p, direction, coordinate):
        """Check if error 5p-3p is of zero length due to hitting start or end of sequence"""
        out = False
        if self.is_plus_strand:
            if direction == '5p':
                if error_5p == error_3p == 0:
                    out = True
            elif direction == '3p':
                if error_5p == error_3p == coordinate.length:
                    out = True
        else:
            if direction == '5p':
                if error_5p == error_3p == coordinate.length - 1:
                    out = True
            elif direction == '3p':
                if error_5p == error_3p == -1:
                    out = True
        return out

    def _error_border_mark(self, sl, sl_next):
        """Calculates the error border point between two super loci according to the formula
        min(ig_length / 2, sqrt(ig_length) * 10), which is then used for error masks.
        """
        def offset(dist):
            if dist <= 0:
                # could happend with nested genes
                return 0
            return min(dist // 2, int(math.sqrt(dist)) * 10)

        if self.is_plus_strand:
            dist = sl_next.start - sl.end
            mark = sl.end + offset(dist)
        else:
            dist = sl.end - sl_next.start
            mark = sl.end - offset(dist)
        return mark


##### main flow control #####
class ImportController(object):
    def __init__(self, database_path, config={}, replace_db=False):
        self.database_path = database_path
        self.latest_genome = None
        self._mk_session(replace_db)
        # queues for adding to db
        self.insertion_queues = InsertionQueue(session=self.session, engine=self.engine)
        self.config = {'min_intron_length': 20}  # the default config
        self.config.update(config)

    def _mk_session(self, replace_db):
        if os.path.exists(self.database_path):
            if replace_db:
                print('removed existing database at {}'.format(self.database_path))
                os.remove(self.database_path)
            else:
                print('database already existing at {} and --replace-db not set'.format(self.database_path))
                exit()
        self.engine = create_engine(helpers.full_db_path(self.database_path), echo=False)
        orm.Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def make_genome(self, genome_args=None):
        if genome_args is None:
            genome_args = {}
        genome = orm.Genome(**genome_args)
        self.latest_fasta_importer = FastaImporter(genome)
        self.session.add(genome)
        self.session.commit()

    def run_analyze(self):
        # run ANALYZE; on the db for hopefully more performant queries
        logging.info('Running ANALYZE on the database')
        with self.engine.connect() as con:
            con.execute('ANALYZE;')

    def add_genome(self, fasta_path, gff_path, genome_args=None, clean_gff=True):
        if genome_args is None:
            genome_args = {}
        if 'species' in genome_args:
            logging.info(f'Starting to add genome: {genome_args["species"]}')
        else:
            logging.info(f'Starting to add an unnamed genome.')
        logging.info(f'FASTA path: {fasta_path}')
        logging.info(f'GFF path: {gff_path}')

        self.clean_tmp_data()
        self.add_sequences(fasta_path, genome_args)
        try:
            self.add_gff(gff_path, clean=clean_gff)
            self.run_analyze()
        except Exception as e:
            self.session.close()
            part_path = f'{self.database_path}.partial'
            shutil.move(self.database_path, part_path)
            print(f'Aborting due to error, attempt so far saved at {part_path} '
                  f'for debugging purposes', file=sys.stderr)
            raise e

    def add_sequences(self, seq_path, genome_args=None):
        if genome_args is None:
            genome_args = {}
        if self.latest_genome is None:
            self.make_genome(genome_args)

        self.latest_fasta_importer.add_sequences(seq_path)
        self.session.commit()

    def clean_tmp_data(self):
        self.latest_genome = None
        self.latest_super_loci = []

    def add_gff(self, gff_file, clean=True):
        def insert_importer_groups(self, groups):
            """Initiates the calling of the add_to_queue() function of the importers
            in the correct order. Also initiates the insert of the many2many rows.
            """
            for group in groups:
                group['super_locus'].add_to_queue()
                # insert all features as well as transcript and protein related entries
                for transcript in group['transcripts']:
                    # make shortcuts
                    tp = transcript['transcript_piece']
                    tf = transcript['transcript_feature']
                    # add transcript handler that are always present
                    transcript['transcript'].add_to_queue()
                    tp.add_to_queue()
                    tf.add_to_queue()
                    tf.insert_feature_piece_association(tp.id)
                    # if coding transcript
                    if 'protein' in transcript:
                        transcript['protein'].add_to_queue()
                        transcript['protein'].insert_transcript_protein_association(transcript['transcript'].id)
                        transcript['cds'].insert_feature_protein_association(transcript['protein'].id)
                        transcript['cds'].add_to_queue()
                        transcript['cds'].insert_feature_piece_association(tp.id)
                    # if there are introns
                    if 'introns' in transcript:
                        for intron in transcript['introns']:
                            intron.add_to_queue()
                            intron.insert_feature_piece_association(tp.id)
                    # insert the errors
                    for error in transcript['errors']:
                        error.add_to_queue()
                        error.insert_feature_piece_association(tp.id)

        def clean_and_insert(self, groups, clean, is_final_coord):
            plus = [g for g in groups if g['super_locus'].is_plus_strand]
            minus = [g for g in groups if not g['super_locus'].is_plus_strand]
            if clean:
                # check and correct for errors
                # do so for each strand seperately
                # all changes should be made by reference
                GFFErrorHandling(plus, self).resolve_errors()
                # reverse order on minus strand
                GFFErrorHandling(minus[::-1], self).resolve_errors()
            # insert importers
            insert_importer_groups(self, plus)
            insert_importer_groups(self, minus)
            if is_final_coord or self.insertion_queues.total_size() > 10000:
                self.insertion_queues.execute_so_far()

        assert self.latest_fasta_importer is not None, 'No recent genome found'
        logging.info('Starting to parse the GFF file')
        self.latest_fasta_importer.mk_mapper(gff_file)
        gff_organizer = OrganizedGFFEntries(gff_file)
        gff_organizer.load_organized_entries()

        organized_gff_entries = gff_organizer.organized_entries
        n_organized_gff_entries = len(organized_gff_entries)
        geenuff_importer_groups = []
        for i, seqid in enumerate(organized_gff_entries.keys()):
            for entry_group in organized_gff_entries[seqid]:
                organized_entries = OrganizedGFFEntryGroup(entry_group, self.latest_fasta_importer,
                                                           self)
                geenuff_importer_groups.append(organized_entries.get_geenuff_importers())
            # never do error checking across fasta sequence borders
            is_final_coord = (i == (n_organized_gff_entries - 1))
            clean_and_insert(self, geenuff_importer_groups, clean, is_final_coord)
            logging.info(f'Finished importing features from {len(geenuff_importer_groups)} super loci '
                         f'from coordinate with seqid {seqid} ({i + 1}/{n_organized_gff_entries})')
            geenuff_importer_groups = []


class Insertable(ABC):
    @abstractmethod
    def add_to_queue(self):
        pass


class FastaImporter(object):
    def __init__(self, genome):
        self.genome = genome
        self.mapper = None
        self._coords_by_seqid = None
        self._gffid_to_coords = None
        self._gff_seq_ids = None

    @property
    def gffid_to_coords(self):
        if not self._gffid_to_coords:
            self._gffid_to_coords = {}
            for gffid in self._gff_seq_ids:
                fa_id = self.mapper(gffid)
                x = self.coords_by_seqid[fa_id]
                self._gffid_to_coords[gffid] = x
        return self._gffid_to_coords

    @property
    def coords_by_seqid(self):
        if not self._coords_by_seqid:
            self._coords_by_seqid = {c.seqid: c for c in self.genome.coordinates}
        return self._coords_by_seqid

    def mk_mapper(self, gff_file=None):
        fa_ids = [e.seqid for e in self.genome.coordinates]
        if gff_file is not None:  # allow setup without ado when we know IDs match exactly
            self._gff_seq_ids = helpers.get_seqids_from_gff(gff_file)
        else:
            self._gff_seq_ids = fa_ids
        mapper, is_forward = helpers.two_way_key_match(fa_ids, self._gff_seq_ids)
        self.mapper = mapper

        if not is_forward:
            raise NotImplementedError("Still need to implement backward match if fasta IDs "
                                      "are subset of gff IDs")

    def add_sequences(self, seq_file):
        # todo, parallelize sequence & annotation format, then import directly from ~Slice
        for seqid, seq in self.parse_fasta(seq_file):
            coord = orm.Coordinate(sequence=seq,
                                   length=len(seq),
                                   seqid=seqid,
                                   sha1=helpers.sequence_hash(seq),
                                   genome=self.genome)
            logging.info(f'Added coordinate object for FASTA sequence with seqid {seqid} to the queue')

    def parse_fasta(self, seq_file, id_delim=' '):
        fp = fastahelper.FastaParser()
        for fasta_header, seq in fp.read_fasta(seq_file):
            seq = seq.upper()  # this may perform poorly
            seqid = fasta_header.split(id_delim)[0]
            yield seqid, seq


class SuperLocusImporter(Insertable):
    def __init__(self,
                 entry_type,
                 given_name,
                 controller,
                 coord=None,
                 is_plus_strand=None,
                 start=-1,
                 end=-1,
                 fully_erroneous=False):
        self.id = InsertCounterHolder.super_locus()
        self.entry_type = entry_type
        self.given_name = given_name
        self.controller = controller
        # not neccessary for insert but helpful for certain error cases
        self.coord = coord
        self.is_plus_strand = is_plus_strand
        self.start = start
        self.end = end
        self.fully_erroneous = fully_erroneous

    def add_to_queue(self):
        to_add = {'type': self.entry_type, 'given_name': self.given_name, 'id': self.id}
        self.controller.insertion_queues.super_locus.queue.append(to_add)

    def __repr__(self):
        params = {'id': self.id, 'type': self.entry_type, 'given_name': self.given_name}
        return helpers.get_repr('SuperLocusImporter', params)


class FeatureImporter(Insertable):
    def __init__(self,
                 coord,
                 is_plus_strand,
                 feature_type,
                 controller,
                 start=-1,
                 end=-1,
                 given_name=None,
                 phase_5p=0,
                 phase_3p=0,
                 score=None,
                 source=None):
        self.id = InsertCounterHolder.feature()
        self.coord = coord
        self.given_name = given_name
        self.is_plus_strand = is_plus_strand
        self.feature_type = feature_type
        # start/end may have to be adapted to geenuff
        self.start = start
        self.end = end
        self.phase_5p = phase_5p
        self.phase_3p = phase_3p  # only used for error checking
        self.score = score
        self.source = source
        self.start_is_biological_start = True
        self.end_is_biological_end = True
        self.controller = controller

    def add_to_queue(self):
        feature = {
            'id': self.id,
            'type': self.feature_type,
            'given_name': self.given_name,
            'coordinate_id': self.coord.id,
            'is_plus_strand': self.is_plus_strand,
            'score': self.score,
            'source': self.source,
            'phase': self.phase_5p,
            'start': self.start,
            'end': self.end,
            'start_is_biological_start': self.start_is_biological_start,
            'end_is_biological_end': self.end_is_biological_end,
        }
        self.controller.insertion_queues.feature.queue.append(feature)

    def insert_feature_piece_association(self, transcript_piece_id):
        features2pieces = {
            'feature_id': self.id,
            'transcript_piece_id': transcript_piece_id,
        }
        self.controller.insertion_queues.association_transcript_piece_to_feature.\
            queue.append(features2pieces)

    def insert_feature_protein_association(self, protein_id):
        features2protein = {
            'feature_id': self.id,
            'protein_id': protein_id,
        }
        self.controller.insertion_queues.association_protein_to_feature.\
            queue.append(features2protein)

    def set_start_end_from_gff(self, gff_start, gff_end):
        self.start, self.end = get_geenuff_start_end(gff_start, gff_end, self.is_plus_strand)

    def pos_cmp_key(self):
        sortable_start = self.start
        sortable_end = self.end
        if not self.is_plus_strand:
            sortable_start *= -1
            sortable_end *= -1
        return self.coord.seqid, self.is_plus_strand, sortable_start, sortable_end, self.feature_type

    def __repr__(self):
        params = {
            'id': self.id,
            'coord_id': self.coord.id,
            'type': self.feature_type,
            'is_plus_strand': self.is_plus_strand,
            'phase': self.phase_5p,
        }
        if self.given_name:
            params['given_name'] = self.given_name
        return helpers.get_repr('FeatureImporter', params, str(self.start) + '--' + str(self.end))


class TranscriptImporter(Insertable):
    def __init__(self, entry_type, given_name, super_locus_id, controller, longest=False):
        self.id = InsertCounterHolder.transcript()
        self.entry_type = entry_type
        self.given_name = given_name
        self.super_locus_id = super_locus_id
        self.controller = controller
        self.longest = longest

    def add_to_queue(self):
        transcript = self._get_params_dict()
        self.controller.insertion_queues.transcript.queue.append(transcript)

    def _get_params_dict(self):
        d = {
            'id': self.id,
            'type': self.entry_type,
            'given_name': self.given_name,
            'super_locus_id': self.super_locus_id,
            'longest': self.longest,
        }
        return d

    def __repr__(self):
        return helpers.get_repr('TranscriptImporter', self._get_params_dict())


class TranscriptPieceImporter(Insertable):
    def __init__(self, given_name, transcript_id, position, controller):
        self.id = InsertCounterHolder.transcript_piece()
        self.given_name = given_name
        self.transcript_id = transcript_id
        self.position = position
        self.controller = controller

    def add_to_queue(self):
        transcript_piece = self._get_params_dict()
        self.controller.insertion_queues.transcript_piece.queue.append(transcript_piece)

    def _get_params_dict(self):
        d = {
            'id': self.id,
            'given_name': self.given_name,
            'transcript_id': self.transcript_id,
            'position': self.position,
        }
        return d

    def __repr__(self):
        return helpers.get_repr('TranscriptPieceImporter', self._get_params_dict())


class ProteinImporter(Insertable):
    def __init__(self, given_name, super_locus_id, controller):
        self.id = InsertCounterHolder.protein()
        self.given_name = given_name
        self.super_locus_id = super_locus_id
        self.controller = controller

    def add_to_queue(self):
        protein = self._get_params_dict()
        self.controller.insertion_queues.protein.queue.append(protein)

    def _get_params_dict(self):
        d = {
            'id': self.id,
            'given_name': self.given_name,
            'super_locus_id': self.super_locus_id,
        }
        return d

    def insert_transcript_protein_association(self, transcript_id):
        transcript2protein = {
            'transcript_id': transcript_id,
            'protein_id': self.id,
        }

        self.controller.insertion_queues.association_transcript_to_protein.\
            queue.append(transcript2protein)

    def __repr__(self):
        return helpers.get_repr('ProteinImporter', self._get_params_dict())
