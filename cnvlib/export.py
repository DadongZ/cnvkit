"""Export CNVkit objects and files to other formats."""
from __future__ import absolute_import, division, print_function

import collections
import math
import sys

import numpy as np
import pandas as pd
from Bio._py3k import map, range, zip, StringIO

from . import call, core
from .cnary import CopyNumArray as CNA

ProbeInfo = collections.namedtuple('ProbeInfo', 'label chrom start end gene')

def merge_samples(filenames):
    """Merge probe values from multiple samples into a 2D table (of sorts).

    Input:
        dict of {sample ID: (probes, values)}
    Output:
        list-of-tuples: (probe, log2 coverages...)
    """
    handles = []
    datastreams = []  # e.g. [list-of-pairs, list-of-pairs, ...]
    for fname in filenames:
        handle = open(fname)
        handles.append(handle)
        data = core.parse_tsv(handle)
        datastreams.append(data)
    # Emit the individual rows merged across samples, one per probe
    for rows in zip(*datastreams):
        yield merge_rows(rows)
    # Clean up
    for handle in handles:
        handle.close()


def merge_rows(rows):
    """Combine equivalent rows of coverage data across multiple samples.

    Check that probe info matches across all samples, then merge the log2
    coverage values.

    Input: a list of individual rows corresponding to the same probes from
    different coverage files.
    Output: a list starting with the single common Probe object, followed by the
    log2 coverage values from each sample, in order.
    """
    probe_infos, coverages = zip(*map(row_to_probe_coverage, rows))
    probe_info = core.check_unique(probe_infos, "probe Name")
    combined_row = [probe_info] + list(coverages)
    return combined_row


def row_to_probe_coverage(row):
    """Repack a parsed row into a ProbeInfo instance and coverage value."""
    chrom, start, end, gene, coverage = row[:5]
    label = "%s:%s-%s:%s" % (chrom, start, end, gene)
    probe_info = ProbeInfo(label, chrom, int(start), int(end), gene)
    return probe_info, float(coverage)


# Supported formats:

def fmt_cdt(sample_ids, rows):
    """Format as CDT."""
    outheader = ['GID', 'CLID', 'NAME', 'GWEIGHT'] + sample_ids
    header2 = ['AID', '', '', '']
    header2.extend(['ARRY' + str(i).zfill(3) + 'X'
                    for i in range(len(sample_ids))])
    outrows = [header2]
    for i, row in enumerate(rows):
        probe, values = row[0], row[1:]
        outrow = ['GENE%dX' % i, 'IMAGE:%d' % i, probe.label, 1] # or probe.gene?
        outrow.extend(values)
        outrows.append(outrow)
    return outheader, outrows


# TODO
def fmt_gct(sample_ids, rows):
    return NotImplemented


def fmt_jtv(sample_ids, rows):
    """Format for Java TreeView."""
    outheader = ["CloneID", "Name"] + sample_ids
    outrows = [["IMAGE:", row[0].label] + row[1:] for row in rows]
    return outheader, outrows


# TODO
def fmt_multi(sample_ids, rows):
    return NotImplemented


# Special cases

def export_nexus_basic(sample_fname):
    """Biodiscovery Nexus Copy Number "basic" format.

    Only represents one sample per file.
    """
    cnarr = CNA.read(sample_fname)
    out_table = cnarr.data.loc[:, ['chromosome', 'start', 'end', 'gene', 'log2']]
    out_table['probe'] = cnarr.labels()
    return out_table


def export_seg(sample_fnames):
    """SEG format for copy number segments.

    Segment breakpoints are not the same across samples, so samples are listed
    in serial with the sample ID as the left column.
    """
    outrows = []
    chrom_ids = None
    for fname in sample_fnames:
        segments = CNA.read(fname)
        if chrom_ids is None:
            # Create & store
            chrom_ids = create_chrom_ids(segments)
        else:
            # Verify
            core.assert_equal("Segment chromosome names differ",
                              previous=chrom_ids.keys(),
                              current=create_chrom_ids(segments).keys())

        if 'probes' in segments:
            outheader = ["ID", "Chromosome", "Start", "End", "NumProbes", "Mean"]
            def row2out(row):
                return (segments.sample_id, chrom_ids[row['chromosome']],
                        row['start'], row['end'], row['probes'],
                        row['log2'])
        else:
            outheader = ["ID", "Chromosome", "Start", "End", "Mean"]
            def row2out(row):
                return (segments.sample_id, chrom_ids[row['chromosome']],
                        row['start'], row['end'], row['log2'])
        outrows.extend(row2out(row) for row in segments)
    return outheader, outrows


def create_chrom_ids(segments):
    """Map chromosome names to integers in the order encountered."""
    mapping = collections.OrderedDict()
    curr_idx = 1
    for chrom in segments.chromosome:
        if chrom not in mapping:
            mapping[chrom] = curr_idx
            curr_idx += 1
    return mapping


# _____________________________________________________________________________
# BED

def export_bed(sample_fnames, args):
    """Export to BED format.

    For each region in each sample which does not have neutral copy number
    (equal to 2 or the value set by --ploidy), the columns are:

        - reference sequence name
        - start (0-indexed)
        - end
        - sample name or given label
        - integer copy number
    """
    bed_rows = []
    for fname in sample_fnames:
        segs = CNA.read(fname)
        rows = segments2bed(segs, args.sample_id or segs.sample_id, args.ploidy,
                            args.male_reference, args.show_neutral)
        bed_rows.extend(rows)
    return None, bed_rows


def segments2bed(segments, label, ploidy, is_reference_male, show_neutral):
    """Convert a copy number array to a BED-like format."""
    absolutes = call.absolute_pure(segments, ploidy, is_reference_male)
    for row, abs_val in zip(segments, absolutes):
        ncopies = int(round(abs_val))
        # Ignore regions of neutral copy number
        if show_neutral or ncopies != ploidy:
            yield (row["chromosome"], row["start"], row["end"], label, ncopies)


# _____________________________________________________________________________
# theta

def export_theta(tumor, reference):
    """Convert tumor segments and normal .cnr or reference .cnn to THetA input.

    Follows the THetA segmentation import script but avoid repeating the
    pileups, since we already have the mean depth of coverage in each target
    bin.

    The options for average depth of coverage and read length do not matter
    crucially for proper operation of THetA; increased read counts per bin
    simply increase the confidence of THetA's results.

    THetA2 input format is tabular, with columns:
        ID, chrm, start, end, tumorCount, normalCount

    where chromosome IDs ("chrm") are integers 1 through 24.
    """
    tumor_segs = CNA.read(tumor)
    ref_vals = CNA.read(reference)

    outheader = ["#ID", "chrm", "start", "end", "tumorCount", "normalCount"]
    outrows = []
    # Convert chromosome names to 1-based integer indices
    prev_chrom = None
    chrom_id = 0
    for seg, ref_rows in ref_vals.by_segment(tumor_segs):
        if seg["chromosome"] != prev_chrom:
            chrom_id += 1
            prev_chrom = seg["chromosome"]
        fields = calculate_theta_fields(seg, ref_rows, chrom_id)
        outrows.append(fields)

    return outheader, outrows


def calculate_theta_fields(seg, ref_rows, chrom_id):
    """Convert a segment's info to a row of THetA input.

    For the normal/reference bin count, take the mean of the bin values within
    each segment so that segments match between tumor and normal.
    """
    # These two scaling factors don't meaningfully affect THetA's calculation
    # unless they're too small
    expect_depth = 100  # Average exome-wide depth of coverage
    read_length = 100
    # Similar number of reads in on-, off-target bins; treat them equally
    segment_size = 1000 * seg["probes"]

    def logratio2count(log2_ratio):
        """Calculate a segment's read count from log2-ratio.

        Math:
            nbases = read_length * read_count
        and
            nbases = segment_size * read_depth
        where
            read_depth = read_depth_ratio * expect_depth

        So:
            read_length * read_count = segment_size * read_depth
            read_count = segment_size * read_depth / read_length
        """
        read_depth = (2 ** log2_ratio) * expect_depth
        read_count = segment_size * read_depth / read_length
        return int(round(read_count))

    tumor_count = logratio2count(seg["log2"])
    ref_count = logratio2count(ref_rows["log2"].mean())
    # e.g. "start_1_93709:end_1_19208166"
    row_id = ("start_%d_%d:end_%d_%d"
              % (chrom_id, seg["start"], chrom_id, seg["end"]))
    return (row_id,       # ID
            chrom_id,     # chrm
            seg["start"], # start
            seg["end"],   # end
            tumor_count,  # tumorCount
            ref_count     # normalCount
           )


# _____________________________________________________________________________
# VCF

VCF_HEADER = """\
##fileformat=VCFv4.0
##INFO=<ID=CIEND,Number=2,Type=Integer,Description="Confidence interval around END for imprecise variants">
##INFO=<ID=CIPOS,Number=2,Type=Integer,Description="Confidence interval around POS for imprecise variants">
##INFO=<ID=END,Number=1,Type=Integer,Description="End position of the variant described in this record">
##INFO=<ID=IMPRECISE,Number=0,Type=Flag,Description="Imprecise structural variation">
##INFO=<ID=SVLEN,Number=-1,Type=Integer,Description="Difference in length between REF and ALT alleles">
##INFO=<ID=SVTYPE,Number=1,Type=String,Description="Type of structural variant">
##ALT=<ID=DEL,Description="Deletion">
##ALT=<ID=DUP,Description="Duplication">
##ALT=<ID=CNV,Description="Copy number variable region">
##FORMAT=<ID=GT,Number=1,Type=Integer,Description="Genotype">
##FORMAT=<ID=GQ,Number=1,Type=Float,Description="Genotype quality">
##FORMAT=<ID=CN,Number=1,Type=Integer,Description="Copy number genotype for imprecise events">
##FORMAT=<ID=CNQ,Number=1,Type=Float,Description="Copy number genotype quality for imprecise events">
"""
# #CHROM  POS   ID  REF ALT   QUAL  FILTER  INFO  FORMAT  NA00001
# 1 2827693   . CCGTGGATGCGGGGACCCGCATCCCCTCTCCCTTCACAGCTGAGTGACCCACATCCCCTCTCCCCTCGCA  C . PASS  SVTYPE=DEL;END=2827680;BKPTID=Pindel_LCS_D1099159;HOMLEN=1;HOMSEQ=C;SVLEN=-66 GT:GQ 1/1:13.9
# 2 321682    . T <DEL>   6 PASS    IMPRECISE;SVTYPE=DEL;END=321887;SVLEN=-105;CIPOS=-56,20;CIEND=-10,62  GT:GQ 0/1:12
# 3 12665100  . A <DUP>   14  PASS  IMPRECISE;SVTYPE=DUP;END=12686200;SVLEN=21100;CIPOS=-500,500;CIEND=-500,500   GT:GQ:CN:CNQ  ./.:0:3:16.2
# 4 18665128  . T <DUP:TANDEM>  11  PASS  IMPRECISE;SVTYPE=DUP;END=18665204;SVLEN=76;CIPOS=-10,10;CIEND=-10,10  GT:GQ:CN:CNQ  ./.:0:5:8.3


def export_vcf(sample_fname, ploidy, is_reference_male, sample_id=None):
    """Convert segments to Variant Call Format.

    For now, only 1 sample per VCF. (Overlapping CNVs seem tricky.)

    Spec: https://samtools.github.io/hts-specs/VCFv4.2.pdf
    """
    segments = CNA.read(sample_fname)
    vcf_columns = ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER",
                   "INFO", "FORMAT", sample_id or segments.sample_id]
    vcf_rows = [(chrom, posn, '.', 'N', "<%s>" % alt, '.', '.', info, fmts, gtype)
                for chrom, posn, alt, info, fmts, gtype in
                segments2vcf(segments, ploidy, is_reference_male)]
    table = pd.DataFrame.from_records(vcf_rows, columns=vcf_columns)
    vcf_body = table.to_csv(sep='\t', header=True, index=False,
                            float_format="%.3g")
    return VCF_HEADER, vcf_body


# XXX refactor with segments2bed; return a DataFrame with all info
def segments2vcf(segments, ploidy, is_reference_male):
    """Convert copy number segments to VCF records."""
    absolutes = call.absolute_pure(segments, ploidy, is_reference_male)
    for row, abs_val in zip(segments, absolutes):
        ncopies = int(round(abs_val))
        if ncopies == ploidy:
            # Skip regions of neutral copy number
            continue  # or "CNV" for subclonal?

        svlen = row["end"] - row["start"]
        if ncopies > ploidy:
            svtype = "DUP"
            formats = "GT:GQ:CN:CNQ"
            genotype = "0/1:0:%d:%g" % (ncopies, row["probes"])
        elif ncopies < ploidy:
            svtype = "DEL"
            svlen *= -1
            formats = "GT:GQ"
            # TODO XXX handle non-diploid ploidies, haploid chroms
            if ncopies == 0:
                # Complete deletion, 0 copies
                gt = "1/1"
            else:
                # Single copy deletion
                gt = "0/1"
            genotype = "%s:%d" % (gt, row["probes"])

        # INFO
        info = ";".join(["IMPRECISE",
                         "SVTYPE=%s" % svtype,
                         "END=%d" % row["end"],
                         "SVLEN=%d" % svlen,
                         # CIPOS=-56,20;CIEND=-10,62
                        ])

        yield (row["chromosome"], max(1, row["start"]), svtype, info, formats,
                genotype)


# _____________________________________________________________________________

EXPORT_FORMATS = {
    'cdt': fmt_cdt,
    # 'gct': fmt_gct,
    'jtv': fmt_jtv,
    'nexus-basic': export_nexus_basic,
    # 'nexus-multi1': fmt_multi,
    'seg': export_seg,
    'theta': export_theta,
    'vcf': export_vcf,
}
