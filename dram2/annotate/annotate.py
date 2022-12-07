"""
Main control point for the annotation process

"""
import re
import io
import time
import warnings
import logging
from glob import glob
from functools import partial, reduce
from datetime import datetime
from skbio.io import read as read_sequence
from skbio.io import write as write_sequence
from skbio import Sequence
from skbio.metadata import IntervalMetadata
from os import path, mkdir, stat
from shutil import rmtree, copy2
import pandas as pd
import numpy as np

from pkg_resources import resource_filename


import importlib
import pkgutil
import dram2.db_kits

DB_KITS = [
    importlib.import_module(i.name)
    for i in pkgutil.iter_modules(dram2.db_kits.__path__, dram2.db_kits.__name__ + ".")
]


from dram2.utils.utils import (
    run_process,
    make_mmseqs_db,
    merge_files,
    multigrep,
    remove_suffix,
    setup_logger,
    run_hmmscan,
    sig_scores,
    get_sig_row,
    generic_hmmscan_formater,
    get_reciprocal_best_hits,
    get_best_hits,
    BOUTFMT6_COLUMNS,
)
from dram2.utils.database_handler import DatabaseHandler

# TODO Exceptions are not fully handled
# TODO Distillate sheets is part of the config, drop it
# TODO: add ability to take into account multiple best hits as in old_code.py
# TODO: add silent mode
# TODO: add abx resistance genes
# TODO: in annotated gene faa checkout out ko id for actual kegg gene id
# TODO: add ability to handle [] in file names

MAG_DBS_TO_ANNOTATE = (
    "kegg",
    "kofam_hmm",
    "kofam_ko_list",
    "uniref",
    "peptidase",
    "pfam",
    "dbcan",
    "vogdb",
)
for kit in DB_KITS:
    MAG_DBS_TO_ANNOTATE += tuple(kit.SETTINGS.keys())


def filter_fasta(fasta_loc, min_len=5000, output_loc=None):
    """Removes sequences shorter than a set minimum from fasta files, outputs an object or to a file"""
    kept_seqs = (
        seq for seq in read_sequence(fasta_loc, format="fasta") if len(seq) >= min_len
    )
    if output_loc is None:
        return list(kept_seqs)
    else:
        write_sequence(kept_seqs, format="fasta", into=output_loc)


def run_prodigal(
    fasta_loc, output_dir, logger, mode="meta", trans_table="11", verbose=False
):
    """Runs the prodigal gene caller on a given fasta file, outputs resulting files to given directory"""
    output_gff = path.join(output_dir, "genes.gff")
    output_fna = path.join(output_dir, "genes.fna")
    output_faa = path.join(output_dir, "genes.faa")

    run_process(
        [
            "prodigal",
            "-i",
            fasta_loc,
            "-p",
            mode,
            "-g",
            trans_table,
            "-f",
            "gff",
            "-o",
            output_gff,
            "-a",
            output_faa,
            "-d",
            output_fna,
        ],
        logger,
        verbose=verbose,
    )
    return output_gff, output_fna, output_faa


def get_best_hits(
    query_db,
    target_db,
    output_dir=".",
    query_prefix="query",
    target_prefix="target",
    bit_score_threshold=60,
    threads=10,
    verbose=False,
):
    """Uses mmseqs2 to do a blast style search of a query db against a target db, filters to only include best hits
    Returns a file location of a blast out format 6 file with search results
    """
    # make query to target db
    tmp_dir = path.join(output_dir, "tmp")
    query_target_db = path.join(
        output_dir, "%s_%s.mmsdb" % (query_prefix, target_prefix)
    )
    run_process(
        [
            "mmseqs",
            "search",
            query_db,
            target_db,
            query_target_db,
            tmp_dir,
            "--threads",
            str(threads),
        ],
        verbose=verbose,
    )
    # filter query to target db to only best hit
    query_target_db_top = path.join(
        output_dir, "%s_%s.tophit.mmsdb" % (query_prefix, target_prefix)
    )
    run_process(
        [
            "mmseqs",
            "filterdb",
            query_target_db,
            query_target_db_top,
            "--extract-lines",
            "1",
        ],
        verbose=verbose,
    )
    # filter query to target db to only hits with min threshold
    query_target_db_top_filt = path.join(
        output_dir,
        "%s_%s.tophit.minbitscore%s.mmsdb"
        % (query_prefix, target_prefix, bit_score_threshold),
    )
    run_process(
        [
            "mmseqs",
            "filterdb",
            "--filter-column",
            "2",
            "--comparison-operator",
            "ge",
            "--comparison-value",
            str(bit_score_threshold),
            "--threads",
            str(threads),
            query_target_db_top,
            query_target_db_top_filt,
        ],
        verbose=verbose,
    )
    # convert results to blast outformat 6
    forward_output_loc = path.join(
        output_dir, "%s_%s_hits.b6" % (query_prefix, target_prefix)
    )
    run_process(
        [
            "mmseqs",
            "convertalis",
            query_db,
            target_db,
            query_target_db,
            forward_output_loc,
            "--threads",
            str(threads),
        ],
        verbose=verbose,
    )
    return forward_output_loc


def get_reciprocal_best_hits(
    query_db,
    target_db,
    output_dir=".",
    query_prefix="query",
    target_prefix="target",
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    threads=10,
    verbose=False,
):
    """Take results from best hits and use for a reciprocal best hits search"""
    # TODO: Make it take query_target_db as a parameter
    # create subset for second search
    query_target_db_top_filt = path.join(
        output_dir,
        "%s_%s.tophit.minbitscore%s.mmsdb"
        % (query_prefix, target_prefix, bit_score_threshold),
    )  # I DON'T LIKE THIS
    query_target_db_filt_top_swapped = path.join(
        output_dir,
        "%s_%s.minbitscore%s.tophit.swapped.mmsdb"
        % (query_prefix, target_prefix, bit_score_threshold),
    )
    # swap queries and targets in results database
    run_process(
        [
            "mmseqs",
            "swapdb",
            query_target_db_top_filt,
            query_target_db_filt_top_swapped,
            "--threads",
            str(threads),
        ],
        verbose=verbose,
    )
    target_db_filt = path.join(output_dir, "%s.filt.mmsdb" % target_prefix)
    # create a subdatabase of the target database with the best hits as well as the index of the target database
    run_process(
        [
            "mmseqs",
            "createsubdb",
            query_target_db_filt_top_swapped,
            target_db,
            target_db_filt,
        ],
        verbose=verbose,
    )
    run_process(
        [
            "mmseqs",
            "createsubdb",
            query_target_db_filt_top_swapped,
            "%s_h" % target_db,
            "%s_h" % target_db_filt,
        ],
        verbose=verbose,
    )

    return get_best_hits(
        target_db_filt,
        query_db,
        output_dir,
        target_prefix,
        query_prefix,
        rbh_bit_score_threshold,
        threads,
        verbose,
    )


def process_reciprocal_best_hits(
    forward_output_loc, reverse_output_loc, target_prefix="target"
):
    """Process the forward and reverse best hits results to find reverse best hits
    Returns the query gene, target gene, if it was a reverse best hit, % identity, bit score and e-value
    """
    forward_hits = pd.read_csv(
        forward_output_loc, sep="\t", header=None, names=BOUTFMT6_COLUMNS
    )
    forward_hits = forward_hits.set_index("qId")
    reverse_hits = pd.read_csv(
        reverse_output_loc, sep="\t", header=None, names=BOUTFMT6_COLUMNS
    )
    reverse_hits = reverse_hits.set_index("qId")

    def check_hit(row: pd.Series):
        rbh = False
        if row.tId in reverse_hits.index:
            rbh = row.name == reverse_hits.loc[row.tId].tId
        return {
            "%s_hit" % target_prefix: row.tId,
            "%s_RBH" % target_prefix: rbh,
            "%s_identity" % target_prefix: row.seqIdentity,
            "%s_bitScore" % target_prefix: row.bitScore,
            "%s_eVal" % target_prefix: row.eVal,
            "index": row.name,
        }

    hits = forward_hits.apply(check_hit, axis=1, result_type="expand")
    # NOTE these lines may not be necessary
    hits.set_index("index", drop=True, inplace=True)
    hits.index.name = None
    return hits


def get_uniref_description(uniref_hits, header_dict):
    """Gets UniRef ID's, taxonomy and full string from list of UniRef IDs for output in annotations"""
    gene_description = list()
    uniref_list = list()
    gene_taxonomy = list()
    for uniref_hit in uniref_hits.uniref_hit:
        header = header_dict[uniref_hit]
        gene_description.append(header)
        uniref_list.append(header[header.find("RepID=") + 6 :])
        gene_taxonomy.append(re.search(r"Tax=(.*?) (\S*?)=", header).group(1))
    new_df = pd.DataFrame(
        [uniref_list, gene_description, gene_taxonomy],
        index=["uniref_id", "uniref_hit", "uniref_taxonomy"],
        columns=uniref_hits.index,
    )
    return pd.concat(
        [new_df.transpose(), uniref_hits.drop("uniref_hit", axis=1)], axis=1, sort=False
    )


def get_peptidase_description(peptidase_hits, header_dict):
    peptidase_list = list()
    peptidase_family = list()
    peptidase_descirption = list()
    for peptidase_hit in peptidase_hits.peptidase_hit:
        header = header_dict[peptidase_hit]
        peptidase_list.append(peptidase_hit)
        peptidase_family.append(re.search(r"#\w*.#", header).group()[1:-1])
        peptidase_descirption.append(header)
    new_df = pd.DataFrame(
        [peptidase_list, peptidase_family, peptidase_descirption],
        index=["peptidase_id", "peptidase_family", "peptidase_hit"],
        columns=peptidase_hits.index,
    )
    return pd.concat(
        [new_df.transpose(), peptidase_hits.drop("peptidase_hit", axis=1)],
        axis=1,
        sort=False,
    )


def run_mmseqs_profile_search(
    query_db,
    pfam_profile,
    output_loc,
    logger,
    output_prefix="mmpro_results",
    db_handler=None,
    threads=10,
    verbose=False,
):
    """Use mmseqs to run a search against pfam, currently keeping all hits and not doing any extra filtering"""
    tmp_dir = path.join(output_loc, "tmp")
    output_db = path.join(output_loc, "%s.mmsdb" % output_prefix)
    run_process(
        [
            "mmseqs",
            "search",
            query_db,
            pfam_profile,
            output_db,
            tmp_dir,
            "-k",
            "5",
            "-s",
            "7",
            "--threads",
            str(threads),
        ],
        logger,
        verbose=verbose,
    )
    output_loc = path.join(output_loc, "%s_output.b6" % output_prefix)
    run_process(
        ["mmseqs", "convertalis", query_db, pfam_profile, output_db, output_loc],
        logger,
        verbose=verbose,
    )
    pfam_results = pd.read_csv(
        output_loc, sep="\t", header=None, names=BOUTFMT6_COLUMNS
    )
    if pfam_results.shape[0] > 0:
        pfam_dict = dict()
        if db_handler is not None:
            pfam_descriptions = db_handler.get_descriptions(
                set(pfam_results.tId), "%s_description" % output_prefix
            )
        else:
            pfam_descriptions = {}
        for gene, pfam_frame in pfam_results.groupby("qId"):
            if len(pfam_descriptions) < 1:
                pfam_dict[gene] = "; ".join(pfam_frame.tId)
            else:
                pfam_dict[gene] = "; ".join(
                    [
                        "%s [%s]" % (pfam_descriptions[ascession], ascession)
                        for ascession in pfam_frame.tId
                    ]
                )
        return pd.DataFrame(pfam_dict, index=[f"{output_prefix}_hits"]).T
    else:
        return pd.DataFrame(columns=[f"{output_prefix}_hits"])


def find_best_dbcan_hit(genome: str, group: pd.DataFrame):
    group["perc_cov"] = group.apply(
        lambda x: (x["target_end"] - x["target_start"]) / x["target_length"], axis=1
    )
    group.sort_values("perc_cov", inplace=True)
    group.columns
    group.sort_values("full_evalue", inplace=True)
    return group.iloc[0]["target_id"]


def dbcan_hmmscan_formater(hits: pd.DataFrame, db_name: str, db_handler=None):
    """
    format the ouput of the dbcan database.

    Note these changes
    introduce new column for best hit from cazy database- this will be the best hit above the already established threshold (0.35 coverage, e-18 evalue) - then for the distillate pull info from the best hit only
    introduce new column for corresponding EC number information from sub families (EC numbers are subfamily ECs)
    Make sure that ids and descriptions are separate (descriptions these are family based)
    :param hits:
    :param db_name:
    :param db_handler:
    :returns:
    """
    hits_sig = hits[hits.apply(partial(get_sig_row, evalue_lim=1e-18), axis=1)]
    if len(hits_sig) == 0:
        # if nothing significant then return nothing, don't get descriptions
        return pd.DataFrame()
    hit_groups = hits_sig.groupby("query_id")
    all_hits = hit_groups.apply(
        lambda x: "; ".join(x["target_id"].apply(lambda y: y[:-4]).unique())
    )
    hits_df = pd.DataFrame(all_hits)
    hits_df.columns = [f"{db_name}_ids"]

    def description_pull(x: str):
        id_list = ([re.findall("^[A-Z]*[0-9]*", str(x))[0] for x in x.split("; ")],)
        id_list = [y for x in id_list for y in x if len(x) > 0]
        description_list = db_handler.get_descriptions(
            id_list, "dbcan_description"
        ).values()
        description_str = "; ".join(description_list)
        return description_str

    if db_handler is not None:
        hits_df[f"{db_name}_hits"] = hits_df[f"{db_name}_id"].apply(description_pull)
        hits_df[f"{db_name}_subfam_ec"] = hits_df[f"{db_name}_ids"].apply(
            lambda x: "; ".join(
                db_handler.get_descriptions(
                    x.split("; "), "dbcan_description", description_name="ec"
                ).values()
            )
        )
    hits_df[f"{db_name}_best_hit"] = [find_best_dbcan_hit(*i) for i in hit_groups]
    hits_df.rename_axis(None, inplace=True)
    hits_df.columns
    return hits_df


def kofam_hmmscan_formater(
    hits: pd.DataFrame,
    hmm_info_path: str = None,
    use_dbcan2_thresholds: bool = False,
    top_hit: bool = True,
):
    hmm_info = pd.read_csv(hmm_info_path, sep="\t", index_col=0)
    if use_dbcan2_thresholds:
        hits_sig = hits[hits.apply(get_sig_row, axis=1)]
    else:
        hits_sig = sig_scores(hits, hmm_info)
    # if there are any significant results then parse to dataframe
    if len(hits_sig) == 0:
        return pd.DataFrame()
    kegg_dict = dict()
    for gene, frame in hits_sig.groupby("query_id"):
        # TODO: take top hit for full length genes and all hits for domains?
        # TODO: if top hit then give all e-value and bitscore info
        if top_hit:
            best_hit = frame[frame.full_evalue == frame.full_evalue.min()]
            ko_id = best_hit["target_id"].iloc[0]
            kegg_dict[gene] = [ko_id, hmm_info.loc[ko_id, "definition"]]
        else:
            kegg_dict[gene] = [
                ",".join([i for i in frame.target_id]),
                "; ".join([hmm_info.loc[i, "definition"] for i in frame.target_id]),
            ]
    return pd.DataFrame(kegg_dict, index=["ko_id", "kegg_hit"]).transpose()


def vogdb_hmmscan_formater(hits: pd.DataFrame, db_name: str, db_handler=None):
    hits_sig = hits[hits.apply(get_sig_row, axis=1)]
    if len(hits_sig) == 0:
        # if nothing significant then return nothing, don't get descriptions
        return pd.DataFrame()
    # Get the best hits
    hits_best = hits_sig.sort_values("full_evalue").drop_duplicates(subset=["query_id"])
    if db_handler is None:
        hits_df = hits_best[["target_id", "query_id"]].copy()
    else:
        # get_descriptions
        desc_col = f"{db_name}_hits"
        descriptions = pd.DataFrame(
            db_handler.get_descriptions(
                hits_best["target_id"].unique(), f"{db_name}_description"
            ),
            index=[desc_col],
        ).T
        categories = descriptions[desc_col].apply(lambda x: x.split("; ")[-1])
        descriptions[f"{db_name}_categories"] = categories.apply(
            lambda x: ";".join(set([x[i : i + 2] for i in range(0, len(x), 2)]))
        )
        descriptions["target_id"] = descriptions.index
        hits_df = pd.merge(
            hits_best[["query_id", "target_id"]], descriptions, on=f"target_id"
        )
    hits_df.set_index("query_id", inplace=True, drop=True)
    hits_df.rename_axis(None, inplace=True)
    hits_df.rename(columns={"target_id": f"{db_name}_id"}, inplace=True)
    return hits_df


def get_gene_data(fasta_loc):
    """Take the prodigal gene headers and get the scaffold that it came from
    Based on idba_ud 'scaffold_#' scaffold names with gene name after
    """
    df_dict = dict()
    for seq in read_sequence(fasta_loc, format="fasta"):
        split_label = seq.metadata["id"].split("_")
        scaffold = "_".join(split_label[:-1])
        gene_position = split_label[-1]
        start_position, end_position, strandedness = seq.metadata["description"].split(
            "#"
        )[1:4]
        df_dict[seq.metadata["id"]] = [
            scaffold,
            int(gene_position),
            int(start_position),
            int(end_position),
            int(strandedness),
        ]
    return pd.DataFrame.from_dict(
        df_dict,
        orient="index",
        columns=[
            "scaffold",
            "gene_position",
            "start_position",
            "end_position",
            "strandedness",
        ],
    )


def get_unannotated(fasta_loc, annotations):
    """Get the genes from the fasta which did not get any annotations"""
    return [
        seq.metadata["id"]
        for seq in read_sequence(fasta_loc, format="fasta")
        if seq.metadata["id"] not in annotations
    ]


def assign_grades(annotations):
    """Grade genes based on reverse best hits to KEGG, UniRef and Pfam"""
    grades = dict()
    for gene, row in annotations.iterrows():
        if row.get("kegg_RBH") is True:
            rank = "A"
        elif row.get("uniref_RBH") is True:
            rank = "B"
        elif not pd.isna(row.get("kegg_hit")) or not pd.isna(row.get("uniref_hit")):
            rank = "C"
        elif (
            not pd.isna(row.get("pfam_hits"))
            or not pd.isna(row.get("cazy_hits"))
            or not pd.isna(row.get("peptidase_hit"))
        ):
            rank = "D"
        else:
            rank = "E"
        grades[gene] = rank
    return pd.DataFrame(grades, index=["rank"]).T


def generate_annotated_fasta(input_fasta, annotations, verbosity="short", name=None):
    """Generates fasta entries with added annotation information to the header of a fasta
    either add best annotation (based on grade) (verbosity = short) or all annotations (verbosity = long)
    """
    for seq in read_sequence(input_fasta, format="fasta"):
        if not seq.metadata["id"] in annotations.index:
            seq.metadata["description"] = ""
            yield seq
            continue
        annotation = annotations.loc[seq.metadata["id"]]
        if "rank" in annotations.columns and verbosity == "short":
            annotation_str = "rank: %s" % annotation["rank"]
            if annotation["rank"] == "A":
                annotation_str += "; %s (db=%s)" % (annotation.kegg_hit, "kegg")
            elif annotation["rank"] == "B":
                annotation_str += "; %s (db=%s)" % (annotation.uniref_hit, "uniref")
            elif annotation["rank"] == "C":
                if "kegg_hit" in annotation:
                    annotation_str += "; %s (db=%s)" % (annotation.kegg_hit, "kegg")
                if "uniref_hit" in annotation:
                    annotation_str += "; %s (db=%s)" % (annotation.uniref_hit, "uniref")
            elif annotation["rank"] == "D":
                annotation_str += "; %s (db=%s)" % (annotation.pfam_hits, "pfam")
            else:
                pass
        else:
            annotation_list = []
            if "rank" in annotations.columns:
                annotation_list += ["rank: %s" % annotation["rank"]]
            if "kegg_hit" in annotations.columns:
                if not pd.isna(annotation.kegg_hit):
                    annotation_list += ["%s (db=%s)" % (annotation.kegg_hit, "kegg")]
            if "uniref_hit" in annotations.columns:
                if not pd.isna(annotation.uniref_hit):
                    annotation_list += [
                        "%s (db=%s)" % (annotation.uniref_hit, "uniref")
                    ]
            if "pfam_hits" in annotations.columns:
                if not pd.isna(annotation.pfam_hits):
                    annotation_list += ["%s (db=%s)" % (annotation.pfam_hits, "pfam")]
            if "bin_taxonomy" in annotations.columns:
                if not pd.isna(annotation.bin_taxonomy):
                    annotation_list += [annotation.bin_taxonomy]
            annotation_str = "; ".join(annotation_list)
        if name is not None:
            seq.metadata["id"] = "%s_%s" % (name, seq.metadata["id"])
        seq.metadata["description"] = annotation_str
        yield seq


def create_annotated_fasta(
    input_fasta, annotations, output_fasta, verbosity="short", name=None
):
    """For use with genes files, added annotations"""
    write_sequence(
        generate_annotated_fasta(input_fasta, annotations, verbosity, name),
        format="fasta",
        into=output_fasta,
    )


def generate_renamed_fasta(input_fasta, prefix):
    """For use with scaffolds files, merges together bins with fasta name added as a prefix to the file"""
    for seq in read_sequence(input_fasta, format="fasta"):
        seq.metadata["id"] = "%s_%s" % (prefix, seq.metadata["id"])
        yield seq


def rename_fasta(input_fasta, output_fasta, prefix):
    """See above"""
    write_sequence(
        generate_renamed_fasta(input_fasta, prefix), format="fasta", into=output_fasta
    )


# TODO: add annotations that don't end with '_id'
def annotate_gff(input_gff, output_gff, annotations, prefix=None):
    """Go through a gff and add a prefix to the scaffold and gene number for all ID's"""
    f = open(input_gff)
    o = open(output_gff, "w")
    for line in f:
        line = line.strip()
        if not line.startswith("#"):
            # replace id with new name
            old_scaffold = line.split("\t")[0]
            match = re.search(r"ID=\d*_\d*;", line)
            gene_number = match.group().split("_")[-1][:-1]
            old_gene_name = "%s_%s" % (old_scaffold, gene_number)
            if prefix is not None:  # add prefix to line name and gene name if given
                line = "%s_%s" % (prefix, line)
                gene_name = "%s_%s" % (prefix, old_gene_name)
            else:
                gene_name = old_gene_name
            line = re.sub(r"ID=\d*_\d*;", "ID=%s;" % gene_name, line)
            # get annotations to add from annotations file and add to end of line
            annotations_to_add = {
                strip_endings(i, ["_id"]): annotations.loc[old_gene_name, i]
                for i in annotations.columns
                if i.endswith("_id")
            }
            database_information = [
                'Dbxref="%s:%s"' % (key.strip(), value.strip().replace(":", "_"))
                for key, values in annotations_to_add.items()
                if not pd.isna(values)
                for value in values.split("; ")
                if value != ""
            ]
            if len(database_information) > 0:
                line += "%s;" % ";".join(database_information)
        o.write("%s\n" % line)


def make_gbk_from_gff_and_fasta(
    gff_loc="genes.gff", fasta_loc="scaffolds.fna", faa_loc="genes.faa", output_gbk=None
):
    # filter scaffolds out of fasta which are not in genes.gff
    gff_frame = pd.read_csv(gff_loc, sep="\t", comment="#", header=None)
    gff_scaffolds = set(gff_frame[0])
    capture_fasta = io.StringIO()
    write_sequence(
        (
            i
            for i in read_sequence(fasta_loc, format="fasta")
            if i.metadata["id"] in gff_scaffolds
        ),
        into=capture_fasta,
        format="fasta",
    )
    # scikit-bio can make a genbank for a concatenated gff and fasta file so make this first
    gff = open(gff_loc)
    aa_records = {i.metadata["id"]: i for i in read_sequence(faa_loc, format="fasta")}
    # the gff with fasta format is the gff then ##FASTA then the fasta
    concat_gff = "%s\n##FASTA\n%s" % (gff.read(), capture_fasta.getvalue())

    # now we can only ready one record from the gff/fasta hybrid at a time
    # read a record at a time till end of the fasta
    genbank_records = ""
    # +1 because there is no \n> for the first line in the file and +1 because we are indexing starting at 1
    for i in range(1, capture_fasta.getvalue().count("\n>") + 1 + 1):
        seq = read_sequence(
            io.StringIO(concat_gff), format="gff3", into=Sequence, seq_num=i
        )
        seq.metadata["LOCUS"] = {
            "locus_name": seq.metadata["id"],
            "size": len(seq),
            "unit": "bp",
            "mol_type": "DNA",
            "shape": "linear",
            "division": "ENV",
            "date": time.strftime("%d-%b-%Y").upper(),
        }
        seq_bounds = (
            seq.interval_metadata.lower_bound,
            seq.interval_metadata.upper_bound,
        )
        for interval in seq.interval_metadata.query([seq_bounds]):
            interval.metadata["gene"] = interval.metadata["ID"]
            if interval.metadata["ID"] in aa_records:
                interval.metadata["translation"] = aa_records[interval.metadata["ID"]]
        # need to capture genbank output so we can combine into a multi-genbank file
        capture_print = io.StringIO()
        seq.write(capture_print, format="genbank")
        genbank_records += capture_print.getvalue()

    # if no output then return as string, otherwise write write to output location
    if output_gbk is None:
        return genbank_records
    else:
        open(output_gbk, "w").write(genbank_records)


def get_dups(columns):
    keep = list()
    seen = list()
    for column in columns:
        if column in seen:
            keep.append(False)
        else:
            seen.append(column)
            keep.append(True)
    return keep


def run_trna_scan(fasta, tmp_dir, fasta_name, logger, threads=10, verbose=True):
    """Run tRNAscan-SE on scaffolds and create a table of tRNAs as a separate output"""
    raw_trnas = path.join(tmp_dir, "raw_trnas.txt")
    run_process(
        ["tRNAscan-SE", "-G", "-o", raw_trnas, "--thread", str(threads), fasta],
        logger,
        verbose=verbose,
    )
    if path.isfile(raw_trnas) and stat(raw_trnas).st_size > 0:
        trna_frame = pd.read_csv(raw_trnas, sep="\t", skiprows=[0, 2])
        trna_frame.columns = [i.strip() for i in trna_frame.columns]
        # if begin.1 or end.1 are in trnas then drop, else drop the second begin or end
        if "Begin.1" in trna_frame.columns:
            trna_frame = trna_frame.drop(["Begin.1"], axis=1)
        if "End.1" in trna_frame.columns:
            trna_frame = trna_frame.drop(["End.1"], axis=1)
        trna_frame = trna_frame.loc[:, get_dups(trna_frame.columns)]
        trna_frame.insert(0, "fasta", fasta_name)
        return trna_frame
    else:
        logger.warning("No tRNAs were detected, no trnas.tsv file will be created.")
        return None


RAW_RRNA_COLUMNS = [
    "scaffold",
    "tool_name",
    "type",
    "begin",
    "end",
    "e-value",
    "strand",
    "empty",
    "note",
]
RRNA_COLUMNS = ["fasta", "begin", "end", "strand", "type", "e-value", "note"]


def run_barrnap(fasta, fasta_name, logger, threads=10, verbose=True):
    raw_rrna_str = run_process(
        ["barrnap", "--threads", str(threads), fasta],
        logger,
        capture_stdout=True,
        check=False,
        verbose=verbose,
    )
    raw_rrna_table = pd.read_csv(
        io.StringIO(raw_rrna_str),
        skiprows=1,
        sep="\t",
        header=None,
        names=RAW_RRNA_COLUMNS,
        index_col=0,
    )
    rrna_table_rows = list()
    for gene, row in raw_rrna_table.iterrows():
        rrna_row_dict = {
            entry.split("=")[0]: entry.split("=")[1] for entry in row["note"].split(";")
        }
        rrna_table_rows.append(
            [
                fasta_name,
                row.begin,
                row.end,
                row.strand,
                rrna_row_dict["Name"].replace("_", " "),
                row["e-value"],
                rrna_row_dict.get("note", ""),
            ]
        )
    if len(raw_rrna_table) > 0:
        return pd.DataFrame(
            rrna_table_rows, index=raw_rrna_table.index, columns=RRNA_COLUMNS
        ).reset_index()
    else:
        logger.warning("No rRNAs were detected, no rrnas.tsv file will be created.")
        return None


def make_trnas_interval(scaffold, row, i):
    if row["Begin"] < row["End"]:
        begin = row["Begin"]
        end = row["End"]
        strand = "+"
    else:
        begin = row["End"]
        end = row["Begin"]
        strand = "-"
    metadata = {
        "source": "tRNAscan-SE",
        "type": "tRNA",
        "score": row["Score"],
        "strand": strand,
        "phase": 0,
        "ID": "%s_tRNA_%s" % (scaffold, i),
        "codon": row["Codon"],
        "product": "tRNA-%s" % row["Type"],
    }
    if not pd.isna(row["Note"]):
        metadata["Note"] = row["Note"]
    return begin, end, metadata


# TODO: make it take an input and output gff location and not overwrite
# TODO: for some reason 1 is getting added to intervals when added to gff
def add_intervals_to_gff(
    annotations_loc, gff_loc, len_dict, interval_function, groupby_column, logger
):
    # get fasta length dict so we can merge, I'd love to be able to get this from somewhere else
    annotation_frame = pd.read_csv(annotations_loc, sep="\t")
    # process trnas to intervals
    annotation_dict = dict()
    for scaffold, frame in annotation_frame.groupby(groupby_column):
        if type(scaffold) is not str:
            scaffold = str(scaffold)
        else:
            scaffold = scaffold.strip()
        im = IntervalMetadata(len_dict[scaffold])
        for i, (_, row) in enumerate(frame.iterrows()):
            i += 1
            begin, end, metadata = interval_function(scaffold, row, i)
            begin -= 1
            if begin < 0:
                begin = 0
                logger.warning(
                    "Interval added to gff started less than zero, set to zero"
                )
            im.add(bounds=[(begin, end)], metadata=metadata)
        annotation_dict[scaffold] = im
    # add trna intervals to gff
    gff = list(read_sequence(gff_loc, format="gff3"))
    with open(gff_loc, "w") as f:
        for scaffold, gff_intervals in gff:
            gff_intervals = IntervalMetadata(len_dict[scaffold], gff_intervals)
            if scaffold in annotation_dict:
                gff_intervals.merge(annotation_dict[scaffold])
                gff_intervals.sort()
            f.write(
                gff_intervals.write(
                    io.StringIO(), format="gff3", seq_id=scaffold
                ).getvalue()
            )


def do_blast_style_search(
    query_db,
    target_db,
    working_dir,
    db_handler,
    formater,
    start_time,
    db_name="database",
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    threads=10,
    verbose=False,
):
    """A convenience function to do a blast style reciprocal best hits search"""
    # Get kegg hits
    print(
        "%s: Getting forward best hits from %s"
        % (str(datetime.now() - start_time), db_name)
    )
    forward_hits = get_best_hits(
        query_db,
        target_db,
        working_dir,
        "gene",
        db_name,
        bit_score_threshold,
        threads,
        verbose=verbose,
    )
    if stat(forward_hits).st_size == 0:
        return pd.DataFrame()
    print(
        "%s: Getting reverse best hits from %s"
        % (str(datetime.now() - start_time), db_name)
    )
    # reverse_hits = get_reciprocal_best_hits(query_db, target_db, working_dir, 'gene', db_name,
    #                                        bit_score_threshold, rbh_bit_score_threshold, threads, verbose=verbose)
    hits = process_reciprocal_best_hits(forward_hits, reverse_hits, db_name)
    print(
        "%s: Getting descriptions of hits from %s"
        % (str(datetime.now() - start_time), db_name)
    )
    if "%s_description" % db_name in db_handler.get_database_names():
        header_dict = db_handler.get_descriptions(
            hits["%s_hit" % db_name], "%s_description" % db_name
        )
    else:
        header_dict = multigrep(
            hits["%s_hit" % db_name], "%s_h" % target_db, "\x00", working_dir
        )
    hits = formater(hits, header_dict)
    return hits


def count_motifs(gene_faa, motif="(C..CH)"):
    motif_count_dict = dict()
    for seq in read_sequence(gene_faa, format="fasta"):
        motif_count_dict[seq.metadata["id"]] = len(list(seq.find_with_regex(motif)))
    return motif_count_dict


def strip_endings(text, suffixes: list):
    for suffix in suffixes:
        if text.endswith(suffix):
            text = text[: len(text) - len(suffix)]
    return text





def process_custom_hmm_cutoffs(custom_hmm_cutoffs_loc, custom_hmm_name, verbose=False):
    if custom_hmm_cutoffs_loc is None:
        return {}
    if custom_hmm_name is None:
        raise ValueError(
            "You can't use the custom_hmm_cutoffs_loc argument without the custom_hmm_name and"
            " custom_hmm_locs aguments specified."
        )
    if len(custom_hmm_cutoffs_loc) != len(custom_hmm_name):
        logger.warning(
            f"Custom hmm cutoffs and descriptions were only provided to the first {len(custom_hmm_cutoffs_loc)}."
            " The rest of the custom hmms will use standard cutoffs and have no descriptions."
        )
    return {custom_hmm_name[i]: j for i, j in enumerate(custom_hmm_cutoffs_loc)}


class Annotation:
    def __init__(
        self,
        name,
        scaffolds,
        genes_faa,
        genes_fna,
        annotations,
        gff=None,
        gbk=None,
        trnas=None,
        rrnas=None,
    ):
        # TODO: get abspath for every input file/dir
        # TODO: check that files exist
        self.name = name
        self.scaffolds_loc = scaffolds
        self.genes_faa_loc = genes_faa
        self.genes_fna_loc = genes_fna
        self.gff_loc = gff
        self.gbk_loc = gbk
        self.annotations_loc = annotations
        self.trnas_loc = trnas
        self.rrnas_loc = rrnas

    def get_annotations(self):
        return pd.read_csv(self.annotations_loc, index_col=0, sep="\t")

    def get_trnas(self):
        return pd.read_csv(self.trnas_loc, sep="\t")

    def get_rrnas(self):
        return pd.read_csv(self.rrnas_loc, sep="\t")


def annotate_orf(
    gene_faa: str,
    use_db: list,
    db_handler,
    tmp_dir: str,
    logger: str,
    custom_db_locs=(),
    custom_hmm_locs=(),
    custom_hmm_cutoffs_locs=(),
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    kofam_use_dbcan2_thresholds=False,
    threads=10,
    verbose=False,
):
    # Get kegg hits

    # print('%s: Turning genes from prodigal to mmseqs2 db' % str(datetime.now() - start_time))
    query_db = path.join(tmp_dir, "gene.mmsdb")
    make_mmseqs_db(
        gene_faa, query_db, logger, create_index=True, threads=threads, verbose=verbose
    )
    arguments = {
        "query_db": query_db,
        "gene_faa": gene_faa,
        "tmp_dir": tmp_dir,
        "logger": logger,
        "threads": threads,
        "verbose": verbose,
        "db_handler": db_handler,
        "bit_score_threshold": bit_score_threshold,
        "rbh_bit_score_threshold": rbh_bit_score_threshold,
        "kofam_use_dbcan2_thresholds": kofam_use_dbcan2_thresholds,
    }
    for i in DB_KITS:
        if i.NAME not in use_db:
            continue
        if not np.all(
            [
                path.exists(db_handler.config["search_databases"][j]["location"])
                for j in i.SETTINGS["search_databases"]
            ]
        ):
            raise ValueError(
                f"Missing databases for {i.NAME_FORMAL}: {[j for j in i.SETTINGS['search_databases']]}"
            )
        yield i.search(**arguments)


def annotate_fasta(
    fasta_loc,
    fasta_name,
    output_dir,
    db_handler,
    logger,
    min_contig_size=5000,
    prodigal_mode="meta",
    trans_table="11",
    custom_db_locs=(),
    custom_hmm_locs=(),
    custom_hmm_cutoffs_locs=(),
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    kofam_use_dbcan2_thresholds=False,
    skip_trnascan=False,
    threads=1,
    rename_bins=True,
    keep_tmp_dir=False,
    verbose=False,
):
    """Annotated a single multifasta file, all file based outputs will be in output_dir"""
    # make temporary directory
    tmp_dir = path.join(output_dir, "tmp")
    mkdir(tmp_dir)

    # filter input fasta
    filtered_fasta = path.join(tmp_dir, "filtered_fasta.fa")
    filter_fasta(fasta_loc, min_contig_size, filtered_fasta)

    if stat(filtered_fasta).st_size == 0:
        logger.warning("No sequences were longer than min_contig_size")
        return None
    # predict ORFs with prodigal
    # TODO: handle when prodigal returns no genes
    gene_gff, gene_fna, gene_faa = run_prodigal(
        filtered_fasta,
        tmp_dir,
        logger,
        mode=prodigal_mode,
        trans_table=trans_table,
        verbose=verbose,
    )

    # annotate ORFs
    annotations = annotate_orfs(
        gene_faa,
        db_handler,
        tmp_dir,
        logger,
        custom_db_locs,
        custom_hmm_locs,
        custom_hmm_cutoffs_locs,
        bit_score_threshold,
        rbh_bit_score_threshold,
        kofam_use_dbcan2_thresholds,
        threads,
        verbose,
    )
    annotations = pd.concat([get_gene_data(gene_faa), annotations], axis=1, sort=False)

    renamed_scaffolds = path.join(output_dir, "scaffolds.annotated.fa")
    if rename_bins:
        # rename scaffolds to match prodigal names
        prefix = fasta_name
        rename_fasta(filtered_fasta, renamed_scaffolds, prefix=prefix)
    else:
        prefix = None
        copy2(filtered_fasta, renamed_scaffolds)

    # generate fna and faa output files with annotations
    annotated_fna = path.join(output_dir, "genes.annotated.fna")
    create_annotated_fasta(gene_fna, annotations, annotated_fna, name=prefix)
    annotated_faa = path.join(output_dir, "genes.annotated.faa")
    create_annotated_fasta(gene_faa, annotations, annotated_faa, name=prefix)
    renamed_gffs = path.join(output_dir, "genes.annotated.gff")
    annotate_gff(gene_gff, renamed_gffs, annotations, prefix=prefix)

    # add fasta name to frame and index, append to list
    annotations.insert(0, "fasta", fasta_name)
    if rename_bins:
        annotations.index = annotations.fasta + "_" + annotations.index
    annotations_loc = path.join(output_dir, "annotations.tsv")
    annotations.to_csv(annotations_loc, sep="\t")

    if not keep_tmp_dir:
        rmtree(tmp_dir)

    return Annotation(
        name=fasta_name,
        scaffolds=renamed_scaffolds,
        genes_faa=annotated_faa,
        genes_fna=annotated_fna,
        gff=renamed_gffs,
        annotations=annotations_loc,
    )


def get_fasta_name(fasta_loc):
    return path.splitext(path.basename(remove_suffix(fasta_loc, ".gz")))[0]


def annotate_fastas(
    fasta_locs,
    output_dir,
    db_handler,
    logger,
    min_contig_size=5000,
    prodigal_mode="meta",
    trans_table="11",
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    custom_db_name=(),
    custom_fasta_loc=(),
    custom_hmm_name=(),
    custom_hmm_loc=(),
    custom_hmm_cutoffs_loc=(),
    kofam_use_dbcan2_thresholds=False,
    skip_trnascan=False,
    rename_bins=True,
    keep_tmp_dir=True,
    threads=10,
    verbose=True,
):
    if not genes_called:
        if prodigal_mode in ["single"]:
            logger.warning(
                "When running prodigal in single mode your bins must have long contigs (average length >3 Kbp), "
                "be long enough (total length > 500 Kbp) and have very low contamination in order for prodigal "
                "training to work well."
            )
    # get database locations
    db_handler = databasehandler()
    db_handler.filter_db_locs(
        low_mem_mode, use_uniref, use_vogdb, master_list=MAG_DBS_TO_ANNOTATE
    )
    # check for no conflicting options/configurations
    tmp_dir = path.join(output_dir, "working_dir")
    mkdir(tmp_dir)

    custom_hmm_locs = process_custom_hmms(custom_hmm_loc, custom_hmm_name, logger)
    custom_hmm_cutoffs_locs = process_custom_hmm_cutoffs(
        custom_hmm_cutoffs_loc, custom_hmm_name
    )
    logger.info("Retrieved database locations and descriptions")

    # iterate over list of fastas and annotate each individually
    annotations_list = list()
    for fasta_loc in fasta_locs:
        # get name of file e.g. /home/shaffemi/my_genome.fa -> my_genome
        fasta_name = get_fasta_name(fasta_loc)
        logger.info("Annotating %s" % fasta_name)
        fasta_dir = path.join(tmp_dir, fasta_name)
        mkdir(fasta_dir)
        annotations_list.append(
            annotate_fasta(
                fasta_loc,
                fasta_name,
                fasta_dir,
                db_handler,
                logger,
                min_contig_size,
                prodigal_mode,
                trans_table,
                custom_db_locs,
                custom_hmm_locs,
                custom_hmm_cutoffs_locs,
                bit_score_threshold,
                rbh_bit_score_threshold,
                kofam_use_dbcan2_thresholds,
                skip_trnascan,
                threads,
                rename_bins,
                keep_tmp_dir,
                verbose,
            )
        )
    logger.info("Annotations complete, processing annotations")

    all_annotations = merge_annotations(annotations_list, output_dir)

    # clean up
    if not keep_tmp_dir:
        rmtree(tmp_dir)
    return all_annotations


def annotate_bins_cmd(
    input_fasta,
    output_dir=".",
    min_contig_size=5000,
    prodigal_mode="meta",
    trans_table="11",
    bit_score_threshold=60,
    rbh_bit_score_threshold=350,
    custom_db_name=(),
    custom_fasta_loc=(),
    custom_hmm_name=(),
    custom_hmm_loc=(),
    custom_hmm_cutoffs_loc=(),
    use_uniref=False,
    use_camper=False,
    use_fegenie=False,
    use_sulphur=False,
    use_vogdb=False,
    kofam_use_dbcan2_thresholds=False,
    skip_trnascan=False,
    gtdb_taxonomy=(),
    checkm_quality=(),
    keep_tmp_dir=True,
    low_mem_mode=False,
    threads=10,
    verbose=True,
):
    rename_bins = True
    fasta_locs = [j for i in input_fasta for j in glob(i)]
    annotate_bins(
        list(set(fasta_locs)),
        output_dir,
        min_contig_size,
        prodigal_mode,
        trans_table,
        bit_score_threshold,
        rbh_bit_score_threshold,
        custom_db_name,
        custom_fasta_loc,
        custom_hmm_name,
        custom_hmm_loc,
        custom_hmm_cutoffs_loc,
        use_uniref,
        use_camper,
        use_fegenie,
        use_sulphur,
        use_vogdb,
        kofam_use_dbcan2_thresholds,
        skip_trnascan,
        gtdb_taxonomy,
        checkm_quality,
        rename_bins,
        keep_tmp_dir,
        low_mem_mode,
        threads,
        verbose,
    )

def merge_gtdb_taxonomy(annotations, gtdb_taxonomy):
        gtdb_taxonomy = pd.concat(
            [pd.read_csv(i, sep="\t", index_col=0) for i in gtdb_taxonomy]
        )
        taxonomy = list()
        taxonomy_missing_bins = list()
        for i in annotations.fasta:
            # add taxonomy
            if i in gtdb_taxonomy.index:
                taxonomy.append(gtdb_taxonomy.loc[i, "classification"])
            else:
                taxonomy.append(i)
                taxonomy_missing_bins.append(i)
        for i in set(taxonomy_missing_bins):
            logger.warning(
                "Bin %s was not found in taxonomy file, replaced with bin name." % i
            )
        annotations["bin_taxonomy"] = taxonomy

def merge_checkm_quality(annotations, checkm_quality):
        checkm_quality = pd.concat(
            [pd.read_csv(i, sep="\t", index_col=0) for i in checkm_quality]
        )
        checkm_quality.index = [
            strip_endings(i, [".fa", ".fasta", ".fna"]) for i in checkm_quality.index
        ]

        completeness = list()
        contamination = list()
        quality_missing_bins = list()
        for i in annotations.fasta:
            # add completeness and contamination
            if i in checkm_quality.index:
                completeness.append(checkm_quality.loc[i, "Completeness"])
                contamination.append(checkm_quality.loc[i, "Contamination"])
            else:
                completeness.append(0)
                contamination.append(100)
                quality_missing_bins.append(i)
            for j in set(quality_missing_bins):
                logger.warning(
                    "Bin %s was not found in quality file, "
                    "replaced with completeness 0 and contamination 100." % j
                )
        annotations["bin_completeness"] = completeness
        annotations["bin_contamination"] = contamination

# TODO: Add force flag to remove output dir if it already exists
# TODO: Add continute flag to continue if output directory already exists
# TODO: make fasta loc either a string or list to remove annotate_bins_cmd and annotate_called_genes_cmd?
def annotate_bins(
    fasta_paths,
    genes_called,
    kofam_use_dbcan2_thresholds=False,
    skip_trnascan=False,
    gtdb_taxonomy=(),
    checkm_quality=(),
    rename_bins=True,
    keep_tmp_dir=True,
    low_mem_mode=False,
    threads=10,
    verbose=True,
    log_file_path: str = None,
):

    mkdir(output_dir)
    if log_file_path is None:
        log_file_path = path.join(output_dir, "Annotation.log")
    logger = logging.getLogger("annotation_log")
    setup_logger(logger, log_file_path)
    logger.info(f"The log file is created at {log_file_path}.")
    try:

        if len(fasta_locs) == 0:
            raise ValueError("Given fasta locations return no paths: %s" % input_fasta)
        fasta_names = [get_fasta_name(i) for i in fasta_locs]
        if len(fasta_names) != len(set(fasta_names)):
            raise ValueError(
                "Genome file names must be unique. At least one name appears twice in this search."
            )
        logger.info("%s FASTAs found" % len(fasta_locs))
        # set up
        db_handler = DatabaseHandler(logger)
        db_handler.filter_db_locs(
            low_mem_mode,
            use_uniref,
            use_camper,
            use_fegenie,
            use_sulphur,
            use_vogdb,
            master_list=MAG_DBS_TO_ANNOTATE,
        )
        db_conf = db_handler.get_settings_str()
        logger.info(
            f"Starting the Annotation of Bins with database configuration: \n {db_conf}"
        )

        # check input


        mkdir(output_dir)

        new_annotations = 
        annotate_fastas(
            fasta_locs,
            genes_called,
            output_dir,
            db_handler,
            logger,
            min_contig_size,
            prodigal_mode,
            trans_table,
            bit_score_threshold,
            rbh_bit_score_threshold,
            custom_db_name,
            custom_fasta_loc,
            custom_hmm_name,
            custom_hmm_loc,
            custom_hmm_cutoffs_loc,
            kofam_use_dbcan2_thresholds,
            skip_trnascan,
            rename_bins,
            keep_tmp_dir,
            threads,
            verbose,
        )
        
        annotations
        # if given add taxonomy information
        if len(gtdb_taxonomy) > 0:
            merge_gtdb_taxonomy(annotations, gtdb_taxonomy)
        # if given add quality information
        if len(checkm_quality) > 0:
            merge_checkm_quality(annotations, gtdb_taxonomy)

        annotations.to_csv(path.join(output_dir, "annotations.tsv"), sep="\t")
    except Exception as e:
        breakpoint()
        logger.critical(e)
        logger.critical("An error ocured! The exicution of DRAM is incompleat")
        raise e

    logger.info("Completed annotations")


def annotate_one_fasta(
    fasta_loc: str,
    partial_annotate_orf,
    tmp_dir: str,
    make_new_faa: bool,
    rename_genes: bool,
) -> str:
    fasta_name = get_fasta_name(fasta_loc)
    fasta_dir = path.join(tmp_dir, fasta_name)
    mkdir(fasta_dir)
    orfs = [orf for orf in partial_annotate_orf(fasta_loc, tmp_dir=fasta_dir)]
    annotations = pd.concat(orfs, axis=1)
    if make_new_faa:
        annotated_faa = path.join(fasta_dir, "genes.faa")
        create_annotated_fasta(fasta_loc, annotations, annotated_faa, name=fasta_name)
        faa_locs.append(annotated_faa)

    # add fasta name to frame and index, write file
    annotations.insert(0, "fasta", fasta_name)
    if rename_genes:
        annotations.index = annotations.fasta + "_" + annotations.index
    annotation_loc = path.join(fasta_dir, "annotations.tsv")
    annotations.to_csv(annotation_loc, sep="\t")
    return annotation_loc


def annotate(
    assembly_fasta_paths: list = (),
    gene_faa_paths: list = (),
    min_contig_size=2500,
    prodigal_mode="meta",
    trans_table="11",
    output_dir: str = ".",
    bit_score_threshold: float = 60,
    rbh_bit_score_threshold: float = 350,
    log_file_path: str = str(None),
    past_annotations_path: str = str(None),
    use_db: list = (),
    custom_db_name: list = (),
    custom_fasta_loc: list = (),
    custom_hmm_loc: list = (),
    custom_hmm_name: list = (),
    custom_hmm_cutoffs_loc: list = (),
    use_uniref: bool = False,
    use_vogdb: bool = False,
    kofam_use_dbcan2_thresholds: bool = False,
    rename_genes: bool = True,
    keep_tmp_dir: bool = True,
    low_mem_mode: bool = False,
    threads: int = 10,
    verbose: bool = True,
    make_new_faa: bool = bool(None),
    dry: bool = False,
):

    # TODO move the logger out of here and put the whole process in a try catch
    # Get a logger
    if log_file_path is None:
        log_file_path = path.join(output_dir, "Annotation.log")
    logger = logging.getLogger("annotation_log")
    setup_logger(logger, log_file_path)
    logger.info(f"The log file is created at {log_file_path}")

    if (len(asembly_fasta_paths) + len(gene_faa_path)) < 1:
        raise
    # get assembly locations
    if len(assembly_fasta_path) > 0:
        fasta_names = [get_fasta_name(i) for i in assembly_fasta_path]
    if len(fasta_names) != len(set(fasta_names)):
        raise ValueError(
            "Genome file names must be unique. At least one name appears twice in this search."
        )

    # check if we are making a new faa
    if make_new_faa is None:
        # We only make a new faa if the user asks or
        # we are not apending to a past annotatons
        make_new_faa = past_annotations_path is None

    if dry:
        logging.info(
            "If this was not a dry run, DRAM would have annotated {len(assembly_fasta_paths)} assembly and {len(gene_faa_path)} gene files. all files are listed bellow"
        )
        logging(f"Assemblies: assembly_fasta_paths")

        return
    # TODO add force
    mkdir(output_dir)

    # get database locations
    db_handler = DatabaseHandler(logger)
    if len(fasta_locs) == 0:
        raise ValueError("Given fasta locations returns no paths: %s" % input_faa)
    logger.info("%s fastas found" % len(fasta_locs))

    db_conf = db_handler.get_settings_str()
    logger.info(
        f"Starting the Annotation of Genes with database configuration: \n {db_conf}"
    )
    # logger.info('Annotation started')

    fasta_names = [get_fasta_name(i) for i in fasta_locs]
    if len(fasta_names) != len(set(fasta_names)):
        raise ValueError(
            "Genome file names must be unique. At least one name appears twice in this search."
        )

    if len(use_db + custom_fasta_loc + custom_hmm_loc) < 1:
        raise ValueError(
            "For some reason there are no database selected to"
            " annotate against. This is most likely a result of"
            " bad arguments."
        )

    # get database locations
    db_handler = DatabaseHandler(logger)

    tmp_dir = path.join(output_dir, "working_dir")
    mkdir(tmp_dir)

    # setup custom databases to be searched
    custom_db_locs = process_custom_dbs(
        custom_fasta_loc,
        custom_db_name,
        path.join(tmp_dir, "custom_dbs"),
        threads,
        verbose,
    )
    custom_hmm_locs = process_custom_hmms(custom_hmm_loc, custom_hmm_name, logger)
    logger.info("Retrieved database locations and descriptions")

    # annotate
    annotation_locs = list()
    faa_locs = list()
    # TODO IO is so slow!!! We need to use more memory and make less files

    partial_annotate_orf = partial(
        annotate_orf,
        use_db=use_db,
        db_handler=db_handler,
        logger=logger,
        custom_db_locs=custom_db_locs,
        custom_hmm_locs=custom_hmm_locs,
        custom_hmm_cutoffs_locs=custom_hmm_cutoffs_locs,
        bit_score_threshold=bit_score_threshold,
        rbh_bit_score_threshold=rbh_bit_score_threshold,
        kofam_use_dbcan2_thresholds=kofam_use_dbcan2_thresholds,
        threads=threads,
        verbose=verbose,
    )
    partial_annotate_one_fasta = partial(
        annotate_one_fasta,
        partial_annotate_orf=partial_annotate_orf,
        tmp_dir=tmp_dir,
        make_new_faa=make_new_faa,
        rename_genes=rename_genes,
    )
    annotation_locs: list = [
        partial_annotate_one_fasta(fasta_loc) for fasta_loc in fasta_locs
    ]
    # merge
    annotation_frames: list = [
        pd.read_csv(i, sep="\t", index_col=0) for i in annotation_locs
    ]
    all_annotations = pd.concat(
        annotation_frames,
        sort=False,
    )
    all_annotations = all_annotations.sort_values("fasta")
    if past_annotations_path is not None:
        all_annotations.drop("fasta", axis=1, inplace=True)
        past_annotations = pd.read_csv(past_annotations_path, sep="\t", index_col=0)
        if set(past_annotations.columns).issubset(set(all_annotations.columns)):
            logger.warning(
                "You have passed an annotations file that containes"
                " columns matching the new annotations. You most"
                " likely are annotating with the same database again."
                " The falowing columns will be replaced in the new"
                " annotations file:\n%s"
                % set(past_annotations.columns).intersection(
                    set(all_annotations.columns)
                )
            )
        past_annotations = past_annotations[
            list(set(past_annotations.columns) - set(all_annotations.columns))
        ]
        all_annotations.index = all_annotations.index.str.removeprefix("genes_")
        # Note we use the old annotations fasts
        all_annotations = pd.merge(
            all_annotations,
            past_annotations,
            how="outer",
            left_index=True,
            right_index=True,
        )
        if len(all_annotations) != len(past_annotations):
            logger.critical(
                "The old and new annotations filles did not merge correctly! Check the new"
                " annotations file for errors. Did you use the corect genes.faa for your annotations?"
            )
    all_annotations.to_csv(path.join(output_dir, "annotations.tsv"), sep="\t")
    if len(faa_locs) > 0:
        merge_files(faa_locs, path.join(output_dir, "genes.faa"))

    # clean up
    if not keep_tmp_dir:
        rmtree(tmp_dir)

    print("Completed annotations")


def annotate_called_genes_with_dbs(
    fasta_locs: list = (),
    output_dir: str = ".",
    bit_score_threshold: float = 60,
    rbh_bit_score_threshold: float = 350,
    past_annotations_path: str = None,
    curated_databases: list = (),
    custom_db_name: list = (),
    custom_fasta_loc: list = (),
    custom_hmm_loc: list = (),
    custom_hmm_name: list = (),
    custom_hmm_cutoffs_loc: list = (),
    use_uniref: bool = False,
    use_vogdb: bool = False,
    kofam_use_dbcan2_thresholds: bool = False,
    rename_genes: bool = True,
    keep_tmp_dir: bool = True,
    low_mem_mode: bool = False,
    threads: int = 10,
    verbose: bool = True,
    make_new_faa: bool = None,
):
    if make_new_faa is None:
        # We only make a new faa if the user asks or
        # we are not apending to a past annotatons
        make_new_faa = past_annotations_path is None

    # set up
    start_time = datetime.now()
    print("%s: Annotation started" % str(datetime.now()))

    if len(curated_databases + custom_fasta_loc + custom_hmm_loc) < 1:
        raise ValueError(
            "For some reason there are no database selected to"
            " annotate against. This is most likely a result of"
            " bad arguments."
        )

    # get database locations
    db_handler = DatabaseHandler()
    # db_handler.filter_db_locs(low_mem_mode, use_uniref, use_vogdb, master_list=MAG_DBS_TO_ANNOTATE)

    mkdir(output_dir)
    tmp_dir = path.join(output_dir, "working_dir")
    mkdir(tmp_dir)

    # setup custom databases to be searched
    custom_db_locs = process_custom_dbs(
        custom_fasta_loc,
        custom_db_name,
        path.join(tmp_dir, "custom_dbs"),
        threads,
        verbose,
    )
    custom_hmm_locs = process_custom_hmms(custom_hmm_loc, custom_hmm_name)
    custom_hmm_cutoffs_locs = process_custom_hmm_cutoffs(
        custom_hmm_cutoffs_loc, custom_hmm_name
    )
    print(
        "%s: Retrieved database locations and descriptions"
        % (str(datetime.now() - start_time))
    )

    # annotate
    annotation_locs = list()
    faa_locs = list()
    # TODO IO is so slow!!! We need to use more memory and make less files
    for fasta_loc in fasta_locs:
        # set up
        fasta_name = get_fasta_name(fasta_loc)
        fasta_dir = path.join(tmp_dir, fasta_name)
        mkdir(fasta_dir)

        # annotate
        annotations = pd.concat(
            [
                orf
                for orf in annotate_orf(
                    fasta_loc,
                    curated_databases,
                    db_handler,
                    fasta_dir,
                    start_time,
                    custom_db_locs,
                    custom_hmm_locs,
                    custom_hmm_cutoffs_locs,
                    bit_score_threshold,
                    rbh_bit_score_threshold,
                    kofam_use_dbcan2_thresholds,
                    threads,
                    verbose,
                )
            ]
        )
        if make_new_faa:
            annotated_faa = path.join(fasta_dir, "genes.faa")
            create_annotated_fasta(
                fasta_loc, annotations, annotated_faa, name=fasta_name
            )
            faa_locs.append(annotated_faa)

        # add fasta name to frame and index, write file
        annotations.insert(0, "fasta", fasta_name)
        if rename_genes:
            annotations.index = annotations.fasta + "_" + annotations.index
        annotation_loc = path.join(fasta_dir, "annotations.tsv")
        annotations.to_csv(annotation_loc, sep="\t")
        annotation_locs.append(annotation_loc)

    # merge
    all_annotations = pd.concat(
        [pd.read_csv(i, sep="\t", index_col=0) for i in annotation_locs], sort=False
    )
    all_annotations = all_annotations.sort_values("fasta")
    if past_annotations_path is not None:
        past_annotations = pd.read_csv(past_annotations_path, sep="\t", index_col=0)
        if set(past_annotations.columns).issubset(set(all_annotations.columns)):
            raise Warning(
                "You have passed an annotations file that containes"
                " columns matching the new annotations. You most"
                " likely are annotating with the same database again."
                " The falowing columns will be replaced in the new"
                " annotations file:\n%s"
                % set(past_annotations.columns).intersection(
                    set(all_annotations.columns)
                )
            )
        all_annotations = all_annotations[
            set(all_annotations.columns) - set(past_annotations.columns)
        ]
        all_annotations = pd.merge(
            all_annotations,
            past_annotations,
            how="outer",
            left_index=True,
            right_index=True,
        )
    all_annotations.to_csv(path.join(output_dir, "annotations.tsv"), sep="\t")
    if len(faa_locs) > 0:
        merge_files(faa_locs, path.join(output_dir, "genes.faa"))

    # clean up
    if not keep_tmp_dir:
        rmtree(tmp_dir)

    print("%s: Completed annotations" % str(datetime.now() - start_time))


def merge_annotations(annotations_list, output_dir, write_annotations=False):
    # merge annotation dicts
    all_annotations = pd.concat(
        [i.get_annotations() for i in annotations_list if i is not None], sort=False
    )
    all_annotations = all_annotations.sort_values(
        ["fasta", "scaffold", "gene_position"]
    )

    # merge gene files
    merge_files(
        [i.genes_fna_loc for i in annotations_list if i is not None],
        path.join(output_dir, "genes.fna"),
    )
    merge_files(
        [i.genes_faa_loc for i in annotations_list if i is not None],
        path.join(output_dir, "genes.faa"),
    )
    merge_files(
        [i.scaffolds_loc for i in annotations_list if i is not None],
        path.join(output_dir, "scaffolds.fna"),
    )
    merge_files(
        [i.gff_loc for i in annotations_list if i is not None],
        path.join(output_dir, "genes.gff"),
        True,
    )
    trnas_locs = [
        i.trnas_loc
        for i in annotations_list
        if i is not None
        if i.trnas_loc is not None
    ]
    if len(trnas_locs) > 0:
        merge_files(trnas_locs, path.join(output_dir, "trnas.tsv"), True)
    rrnas_locs = [
        i.rrnas_loc
        for i in annotations_list
        if i is not None
        if i.rrnas_loc is not None
    ]
    if len(rrnas_locs) > 0:
        merge_files(rrnas_locs, path.join(output_dir, "rrnas.tsv"), True)

    # make output gbk dir
    gbk_dir = path.join(output_dir, "genbank")
    mkdir(gbk_dir)
    for anno in annotations_list:
        if anno is not None:
            # TODO: make annotate_fasta generate a genbank dir and then copy it's contents, get rid of Annotation.name
            if path.isfile(anno.gbk_loc):
                copy2(anno.gbk_loc, path.join(gbk_dir, "%s.gbk" % anno.name))
            else:
                for gbk_loc in glob(path.join(anno.gbk_loc, "*.gbk")):
                    copy2(gbk_loc, gbk_dir)
    if write_annotations:
        all_annotations.to_csv(path.join(output_dir, "annotations.tsv"), sep="\t")
    else:
        return all_annotations


def merge_annotations_cmd(input_dirs, output_dir):
    # make Annotation objects per directory
    annotations_list = list()
    for i, annotation_dir in enumerate(glob(input_dirs)):
        name = "annotation_%s" % i
        scaffolds = path.join(annotation_dir, "scaffolds.fna")
        genes_faa = path.join(annotation_dir, "genes.faa")
        genes_fna = path.join(annotation_dir, "genes.fna")
        gff = path.join(annotation_dir, "genes.gff")
        gbk = path.join(annotation_dir, "genbank")
        annotations = path.join(annotation_dir, "annotations.tsv")
        trnas = path.join(annotation_dir, "trnas.tsv")
        if not path.isfile(trnas):
            logger.warning("No trnas.tsv file found in directory %s" % annotation_dir)
            trnas = None
        rrnas = path.join(annotation_dir, "rrnas.tsv")
        if not path.isfile(rrnas):
            logger.warning("No rrnas.tsv file found in directory %s" % annotation_dir)
            rrnas = None
        annotations_list.append(
            Annotation(
                name=name,
                scaffolds=scaffolds,
                genes_faa=genes_faa,
                genes_fna=genes_fna,
                gff=gff,
                gbk=gbk,
                annotations=annotations,
                trnas=trnas,
                rrnas=rrnas,
            )
        )
    # run merge_annotations
    mkdir(output_dir)
    merge_annotations(annotations_list, output_dir, write_annotations=True)
