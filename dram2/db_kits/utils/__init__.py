"""
================
DBKits Utilities
================


General utils for database objects including the template class
"""
from abc import ABC, abstractmethod
from os import path, stat
from typing import Callable, Union
from typing import Optional
from functools import partial
from pathlib import Path
import logging

import pandas as pd

from dram2.utils import run_process, Fasta

HMMSCAN_ALL_COLUMNS = [
    "query_id",
    "query_ascession",
    "query_length",
    "target_id",
    "target_ascession",
    "target_length",
    "full_evalue",
    "full_score",
    "full_bias",
    "domain_number",
    "domain_count",
    "domain_cevalue",
    "domain_ievalue",
    "domain_score",
    "domain_bias",
    "target_start",
    "target_end",
    "alignment_start",
    "alignment_end",
    "query_start",
    "query_end",
    "accuracy",
    "description",
]
HMMSCAN_COLUMN_TYPES = [
    str,
    str,
    int,
    str,
    str,
    int,
    float,
    float,
    float,
    int,
    int,
    float,
    float,
    float,
    float,
    int,
    int,
    int,
    int,
    int,
    int,
    float,
    str,
]

BOUTFMT6_COLUMNS = [
    "qId",
    "tId",
    "seqIdentity",
    "alnLen",
    "mismatchCnt",
    "gapOpenCnt",
    "qStart",
    "qEnd",
    "tStart",
    "tEnd",
    "eVal",
    "bitScore",
]

FILE_LOCATION_TAG = "location"
DRAM_DATAFOLDER_TAG = "dram_data_folder"
DBKIT_TAG = "db_kits"
SETUP_NOTES_TAG = "setup_notes"
CUSTOM_FASTA_DB_TYPE = "custom_fasta"
CUSTOM_HMM_DB_TYPE = "custom_HMM"
QUERY_PREFIX = "query"
TARGET_PREFIX = "target"
HMM_SCAN_MAX_THREADS = 2

DEFAULT_BIT_SCORE_THRESHOLD: float = 60
DEFAULT_RBH_BIT_SCORE_THRESHOLD: float = 350
DEFAULT_KOFAM_USE_DBCAN2_THRESHOLDS: bool = False
DEFAULT_THREADS: int = 10
DEFAULT_GENES_CALLED: bool = False
DEFAULT_USE: bool = False
DEFAULT_FORCE: bool = False
DEFAULT_KEEP_TMP: bool = False


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


def get_reciprocal_best_hits(
    query_db,
    target_db,
    logger,
    output_dir: str,
    bit_score_threshold: int,
    rbh_bit_score_threshold: int,
    threads,
    query_prefix: str = QUERY_PREFIX,
    target_prefix: str = QUERY_PREFIX,
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
        logger,
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
        logger,
    )
    run_process(
        [
            "mmseqs",
            "createsubdb",
            query_target_db_filt_top_swapped,
            "%s_h" % target_db,
            "%s_h" % target_db_filt,
        ],
        logger,
    )

    return get_best_hits(
        query_db=target_db_filt,
        target_db=query_db,
        logger=logger,
        output_dir=output_dir,
        bit_score_threshold=rbh_bit_score_threshold,
        query_prefix=target_prefix,
        target_prefix=query_prefix,
        threads=threads,
    )


def multigrep(search_terms, search_against, logger, split_char="\n", output="."):
    # TODO: multiprocess this over the list of search terms
    """Search a list of exact substrings against a database, takes name of mmseqs db 
    index with _h to search against"""
    hits_file = path.join(output, "hits.txt")
    with open(hits_file, "w") as f:
        f.write("%s\n" % "\n".join(search_terms))
    results = run_process(
        ["grep", "-a", "-F", "-f", hits_file, search_against],
        logger,
        capture_stdout=True,
    )
    processed_results = [
        i.strip() for i in results.strip().split(split_char) if len(i) > 0
    ]
    # remove(hits_file)
    return {i.split()[0]: i for i in processed_results if i != ""}


def do_blast_style_search(
    query_db,
    target_db,
    working_dir,
    logger,
    db_name,
    bit_score_threshold: int,
    rbh_bit_score_threshold: int,
    threads: int,
):
    """A convenience function to do a blast style reciprocal best hits search"""
    # Get kegg hits
    logger.info("Getting forward best hits from %s" % db_name)
    forward_hits = get_best_hits(
        query_db=query_db,
        target_db=target_db,
        logger=logger,
        output_dir=working_dir,
        bit_score_threshold=bit_score_threshold,
        query_prefix="gene",
        target_prefix=db_name,
        threads=threads,
    )
    if stat(forward_hits).st_size == 0:
        return pd.DataFrame(columns=[f"{db_name}_hit"])
    logger.info("Getting reverse best hits from %s" % db_name)
    reverse_hits = get_reciprocal_best_hits(
        query_db=query_db,
        target_db=target_db,
        logger=logger,
        output_dir=working_dir,
        bit_score_threshold=bit_score_threshold,
        rbh_bit_score_threshold=rbh_bit_score_threshold,
        threads=threads,
        query_prefix="gene",
        target_prefix=db_name,
    )
    hits = process_reciprocal_best_hits(forward_hits, reverse_hits, db_name)
    # if "%s_description" % db_name in db_handler.get_database_names():
    #     header_dict = db_handler.get_descriptions(
    #         hits["%s_hit" % db_name], "%s_description" % db_name
    #     )
    # else:
    return hits


def make_mmseqs_db(
    fasta_loc,
    output_loc,
    logger,
    threads,
    create_index=True,
):
    """Takes a fasta file and makes a mmseqs2 database for use in blast searching and hmm searching with mmseqs2,"""
    run_process(
        ["mmseqs", "createdb", fasta_loc, output_loc],
        logger,
    )
    if create_index:
        tmp_dir = path.join(path.dirname(output_loc), "tmp")
        run_process(
            ["mmseqs", "createindex", output_loc, tmp_dir, "--threads", str(threads)],
            logger,
        )


def run_hmmscan(
    genes_faa: str,
    db_loc: str,
    db_name: str,
    output_loc: str,
    formater: Callable,
    logger: logging.Logger,
    threads: int,
):
    if threads > HMM_SCAN_MAX_THREADS:
        logger.debug(
            f"Something has gone wrong for {db_name}. It is trying to use hmmscan with {threads} threads which is sub-optimal as hmmscan can only make use of {HMM_SCAN_MAX_THREADS} threads."
        )
    output = path.join(output_loc, f"{db_name}_results.unprocessed.b6")
    run_process(
        ["hmmsearch", "--domtblout", output, "--cpu", str(threads), db_loc, genes_faa],
        logger,
    )
    # Parse hmmsearch output
    if not (path.isfile(output) and stat(output).st_size > 0):
        return pd.DataFrame()
    hits = parse_hmmsearch_domtblout(output)
    if len(hits) < 1:
        return pd.DataFrame()
    return formater(hits)


def parse_hmmsearch_domtblout(file):
    df_lines = list()
    for line in open(file):
        if not line.startswith("#"):
            line = line.split()
            line = line[:22] + [" ".join(line[22:])]
            df_lines.append(line)
    hmmsearch_frame = pd.DataFrame(df_lines, columns=HMMSCAN_ALL_COLUMNS)
    for i, column in enumerate(hmmsearch_frame.columns):
        hmmsearch_frame[column] = hmmsearch_frame[column].astype(
            HMMSCAN_COLUMN_TYPES[i]
        )
    return hmmsearch_frame


def get_best_hits(
    query_db: Union[str, Path],
    target_db: Union[str, Path],
    logger: logging.Logger,
    output_dir: Union[str, Path],
    bit_score_threshold,
    threads: int,
    query_prefix=QUERY_PREFIX,
    target_prefix=TARGET_PREFIX,
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
            str(query_db),
            str(target_db),
            str(query_target_db),
            str(tmp_dir),
            "--threads",
            str(threads),
        ],
        logger,
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
        logger,
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
        logger,
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
            query_target_db_top_filt,
            forward_output_loc,
            "--threads",
            str(threads),
        ],
        logger,
    )
    return forward_output_loc


def process_custom_hmm_db_cutoffs(
    custom_hmm_db_cutoffs_loc, custom_hmm_db_name, logger
):
    if custom_hmm_db_cutoffs_loc is None:
        return {}
    if custom_hmm_db_name is None:
        raise ValueError(
            "You can't use the custom_hmm_db_cutoffs_loc argument without the custom_hmm_db_name and"
            " custom_hmm_db_locs aguments specified."
        )
    if len(custom_hmm_db_cutoffs_loc) != len(custom_hmm_db_name):
        logger.warning(
            f"Custom hmm cutoffs and descriptions were only provided to the first {len(custom_hmm_db_cutoffs_loc)}."
            " The rest of the custom hmms will use standard cutoffs and have no descriptions."
        )
    return {custom_hmm_db_name[i]: j for i, j in enumerate(custom_hmm_db_cutoffs_loc)}


def get_basic_descriptions(
    hits: pd.DataFrame, header_dict: dict[str, str], db_name: str
) -> pd.DataFrame:
    """
    Get viral gene full descriptions based on headers (text before first space)
    """
    descriptions: pd.Series = hits[f"{db_name}_hit"].apply(
        lambda x: None if x is None else header_dict[x]
    )
    descriptions.name = f"{db_name}_description"
    return pd.DataFrame(descriptions)


def get_sig_row(row, evalue_lim: float = 1e-15):
    """Check if hmm match is significant, based on dbCAN described parameters"""
    tstart, tend, tlen, evalue = row[
        ["target_start", "target_end", "target_length", "full_evalue"]
    ].values
    perc_cov = (tend - tstart) / tlen
    if perc_cov >= 0.35 and evalue <= evalue_lim:
        return True
    else:
        return False


def generic_hmmscan_formater(
    hits: pd.DataFrame,
    db_name: str,
    hmm_info_path: Optional[Path] = None,
    top_hit: bool = True,
):
    if hmm_info_path is None:
        hmm_info = None
        hits_sig: pd.DataFrame = hits[hits.apply(get_sig_row, axis=1)]
    else:
        hmm_info = pd.read_csv(hmm_info_path, sep="\t", index_col=0)
        hits_sig: pd.DataFrame = sig_scores(hits, hmm_info)
    if len(hits_sig) == 0:
        # if nothing significant then return nothing, don't get descriptions
        return pd.DataFrame()
    if top_hit:
        # Get the best hits
        hits_no_dup = hits_sig.sort_values("full_evalue").drop_duplicates(
            subset=["query_id"]
        )
        if hits_no_dup is None:
            raise ValueError(
                "This error would occurs if removing duplicates caused a None value to be returned. This should be impossible."
            )
        hits_sig = hits_no_dup

    hits_df = hits_sig[["target_id", "query_id"]]
    hits_df.set_index("query_id", inplace=True, drop=True)
    hits_df.rename_axis(None, inplace=True)
    hits_df.columns = [f"{db_name}_id"]
    if hmm_info is not None:
        hits_df = hits_df.merge(
            hmm_info[["definition"]],
            how="left",
            left_on=f"{db_name}_id",
            right_index=True,
        )
        hits_df.rename(columns={"definition": f"{db_name}_hits"}, inplace=True)
    return hits_df


def sig_scores(hits: pd.DataFrame, score_db: pd.DataFrame) -> pd.DataFrame:
    is_sig = list()
    for i, frame in hits.groupby("target_id"):
        row = score_db.loc[i]
        if row["score_type"] == "domain":
            score = frame.domain_score
        elif row["score_type"] == "full":
            score = frame.full_score
        elif row["score_type"] == "-":
            continue
        else:
            raise ValueError(row["score_type"])
        frame = frame.loc[score.astype(float) > float(row.threshold)]
        is_sig.append(frame)
    if len(is_sig) > 0:
        return pd.concat(is_sig)
    else:
        return pd.DataFrame()


class DBKit(ABC):
    """
    DBKit Abstract Class
    ____________________

    Use this as a base class to model all other absract classess off of.

    """

    # this name will apear in lists as the
    name: str = ""
    formal_name: str = ""
    search_type: str = "unknown"
    citation: str = "This database has no citation"
    max_threads: int = -1
    logger: logging.Logger
    working_dir: Path
    bit_score_threshold: int
    rbh_bit_score_threshold: int
    past_annotations_path: str
    kofam_use_dbcan2_thresholds: bool
    threads: int
    make_new_faa: bool
    force: bool
    extra: dict
    config: dict = {}
    selectable: bool = True
    dram_db_loc: Path
    run_set_up = False  # impliment later the ability to setup on the fly
    keep_tmp: bool = False
    has_genome_summary: bool = False
    can_get_ids: bool = True
    location_keys: list[str] = []

    # For updating a counter
    fastas_to_annotate: int = 0
    fastas_annotated: int = 0
    show_percent_of_fastas_done: bool = True
    step_percent_of_fastas_done: int = 10  # no fractions
    percent_of_fastas_done: int = 0  # no fractions

    def set_universals(
        self, name: str, formal_name: str, config: dict, citation: str, db_version: str
    ):
        self.name: str = name
        self.config: dict = config
        self.is_dbkit: bool = True
        self.dram_data_folder: Optional[Path] = None
        self.formal_name: str = formal_name
        self.db_version: str = db_version
        self.citation: str = citation

    def __init__(self, config: dict, logger: logging.Logger):
        self.logger = logger
        if (
            config.get(DBKIT_TAG) is not None
            and config[DBKIT_TAG].get(self.name) is not None
        ):
            self.config = config[DBKIT_TAG][self.name]
        else:
            self.config = {}
        self.dram_data_folder: Optional[Path] = config.get(DRAM_DATAFOLDER_TAG)
        if (
            self.dram_data_folder is not None
            and not self.dram_data_folder.is_absolute()
        ):
            raise ValueError(
                "The data folder path is not none and is not a absolute path. "
                "That should not be possible if it was correctly loaded. Are you "
                "doing your own development?"
            )

    def download(self, user_locations_dict: dict[str, Path]) -> dict[str, Path]:
        pass

    def get_genome_summary(self) -> Optional[Path]:
        return None

    def start_counter(self, fastas_to_annotate: int):
        self.fastas_to_annotate = fastas_to_annotate
        if self.fastas_to_annotate < 10:
            self.show_percent_of_fastas_done = False

    def check_counter_after_annotation(self):
        if self.fastas_to_annotate == 0:
            self.logger.warning(f"Fasta counter not setup {self.formal_name}.")
            return
        if self.fastas_annotated == 0:
            self.logger.info(f"Started annotating gene FASTAs with {self.formal_name}.")
        self.fastas_annotated += 1
        if self.fastas_annotated == self.fastas_to_annotate:
            self.logger.info(
                f"Finished annotating gene FASTAs with {self.formal_name}."
            )
        if self.show_percent_of_fastas_done and (
            (self.fastas_to_annotate // self.fastas_annotated) * 100
        ) >= (self.step_percent_of_fastas_done + self.percent_of_fastas_done):
            self.percent_of_fastas_done += self.step_percent_of_fastas_done
            self.logger.info(
                f"Still annotating gene FASTAs with {self.formal_name}, {self.percent_of_fastas_done}% done."
            )

    def get_config_path(self, required_file: str) -> Path:
        """
        Paths in the config can be complicated. here is a funcion that will get
        you the absolute path, the relieve path or whatever. This should be more
        formalized and the config

        should actualy be maniged in its own structure. With a data file class
        that can use this function.


        """
        if (
            self.config.get(required_file) is None
            or self.config[required_file].get(FILE_LOCATION_TAG) is None
        ):
            if not self.run_set_up:
                raise ValueError(
                    f"The path for {required_file} is required by"
                    f" the Database {self.formal_name} but it has"
                    f" not been configured or was missconfigured"
                    f" in this config provided. the config should includ it"
                    f" like this:\n"
                    f"{DBKIT_TAG}: \n"
                    f"    {self.name}:\n"
                    f"      {required_file}: \n"
                    f"        {FILE_LOCATION_TAG}:"
                )
            else:
                self.logger.warning(
                    f"It looks like {required_file} was not setup, DRAM  will now "
                    "atemp to run setup for {self.formal_name} in order to creat it"
                )
                self.setup()  # it can be asuumed that this will update the config also
        required_path = Path(self.config[required_file]["location"])
        if not required_path.is_absolute() and self.dram_data_folder is not None:
            required_path = self.dram_data_folder / required_path
        elif not required_path.is_absolute() and self.dram_data_folder is None:
            raise ValueError(
                "All paths must be absolute or the DRAM data path can't be "
                "none, but this is not the case for this config and the path "
                f"{required_path}."
            )
        if not required_path.exists():
            if not self.run_set_up:
                raise FileNotFoundError(
                    f"The file {required_file} is not at the path"
                    f" {required_path}. Most likely you moved the DRAM"
                    f" data but forgot to update the config file to"
                    f" point to it. The easy fix is to set the"
                    f" {DRAM_DATAFOLDER_TAG} variable in the config"
                    f" like:\n"
                    f" {DRAM_DATAFOLDER_TAG}: the/path/to/my/file"
                    f" If you are useing full paths and not the"
                    f" {DRAM_DATAFOLDER_TAG} you may want to revue the"
                    f" Configure Dram section of the documentation to"
                    f" make shure your config will work with dram."
                    f" remembere that the config must be a vailid yaml"
                    f" file to work. Also you can alwase use"
                    f" db_bulder to remake your databases and the"
                    f" config file if you don't feel up to editing it"
                    f" yourself."
                )
        return required_path

    def request_config_path(self, required_file: str) -> Optional[Path]:
        """
        Paths in the config can be complicated. here is a funcion that will get
        you the absolute path, the relitve path or whatever. This should be more
        fomalized and the config

        should actualy be maniged in its own structure. With a data file class
        that can use this function.


        """
        if (
            self.config.get(required_file) is None
            or self.config[required_file].get(FILE_LOCATION_TAG) is None
        ):
            if not self.run_set_up:
                return None
        required_path = Path(self.config[required_file]["location"])
        if not required_path.is_absolute() and self.dram_data_folder is not None:
            required_path = self.dram_data_folder / required_path
        elif not required_path.is_absolute() and self.dram_data_folder is None:
            self.logger.debug(
                "All paths must be absolute or the DRAM data path can't be "
                "none, but this is not the case for this config and the path "
                f"{required_path}."
            )
            return None
        if not required_path.exists():
            if not self.run_set_up:
                self.logger.debug(
                    f"The file {required_file} is not at the path"
                    f" {required_path}. Most likely you moved the DRAM"
                    f" data but forgot to update the config file to"
                    f" point to it. The easy fix is to set the"
                    f" {DRAM_DATAFOLDER_TAG} variable in the config"
                    f" like:\n"
                    f" {DRAM_DATAFOLDER_TAG}: the/path/to/my/file"
                    f" If you are useing full paths and not the"
                    f" {DRAM_DATAFOLDER_TAG} you may want to revue the"
                    f" Configure Dram section of the documentation to"
                    f" make shure your config will work with dram."
                    f" remembere that the config must be a vailid yaml"
                    f" file to work. Also you can alwase use"
                    f" db_bulder to remake your databases and the"
                    f" config file if you don't feel up to editing it"
                    f" yourself."
                )
                return None
        return required_path

    @classmethod
    @abstractmethod
    def setup(self):
        pass

    def get_descriptions(self, annotation):
        self.logger.debug(
            f"The get_descriptions function is not separate for the DB {self.name}. This is less efficient than if it is separate and will get in the way of future optimization."
        )
        return pd.DataFrame()

    def get_setup_notes(self) -> dict:
        notes = self.config.get(SETUP_NOTES_TAG)
        if notes is None:
            return {}
        return notes

    def set_args(
        self,
        working_dir: Path,
        # output_dir: Path,
        bit_score_threshold: float = DEFAULT_BIT_SCORE_THRESHOLD,
        rbh_bit_score_threshold: float = DEFAULT_RBH_BIT_SCORE_THRESHOLD,
        kofam_use_dbcan2_thresholds: bool = DEFAULT_KOFAM_USE_DBCAN2_THRESHOLDS,
        threads: int = DEFAULT_THREADS,
        force: bool = DEFAULT_FORCE,
        # extra: dict | None = None,
        # db_path: Path,
        keep_tmp: bool = DEFAULT_KEEP_TMP,
        # "fasta_paths": gene_fasta_paths,
    ):
        self.kofam_use_dbcan2_thresholds: bool = kofam_use_dbcan2_thresholds
        self.working_dir: Path = working_dir / self.name
        self.working_dir.mkdir(exist_ok=True, parents=True)
        # self.output_dir: Path = output_dir
        self.bit_score_threshold: int = bit_score_threshold
        self.rbh_bit_score_threshold: int = rbh_bit_score_threshold
        self.threads: int = threads
        self.force: bool = force
        # self.extra: dict = extra
        # self.db_path = self.setup_db_path(db_path)
        self.keep_tmp: bool = keep_tmp

    @staticmethod
    def setup_db_path(db_path: Path):
        if db_path is None:
            return db_path
        db_path = Path(db_path)
        if db_path.exists():
            return db_path
        else:
            db_path.mkdir(parents=True)

    # def check_on_fly_setup(self):
    #     """
    #     TODO:
    #     - This should be a match but I don't feel like updating today
    #     -

    #     """
    #     if self.config.get("default_db_dir") is None:
    #         self.config["default_db_dir"] = self.dram_db_loc
    #     if self.db_path is None:
    #         self.dram_db_loc = self.config["default_db_dir"]
    #     if self.db_path is None and self.config.get("default_db_dir") is None:
    #         raise ValueError(
    #             "Without a dram_db_directory defided, database can't be built on the fly"
    #         )

    @abstractmethod
    def load_dram_config(self):
        """
        Geting Values out of the dram config
        ____________________________________

        This will be used to check if the database is setup and load vaiables
        or file path from the dram config. Unless you
        overwrite the constructor this functon will be called during
        annotation after the values have been stored. So you can use this to
        check user arguments even read in cusom arguments.
        """
        pass

    def get_settings(self) -> dict:
        """
        Documenting What DRAM does
        __________________________

        We must be able to document what dram does! This is obvious but in the case
        of these many database it is more dificult. We could put all the passed args
        in the log or config but as this program gets more and more complicated so
        will that process and just becouse bit_score_threshold is set by the user
        that unfortunatly dose not mean that the databases will all use it or use it
        in the same whay.

        The best solution is to have each dbkit return the values of the vairiable it uses,
        and this is the method to make that happen. It is up to the maker of the database to
        return all setings that could efect the restult of the run. tools down the line may
        make use of this information to validate themselves.

        This should honestly be easy to do if the rest of the dbkit is setup corectly.

        1. Report the values from any globaly set paramiters. Or values set in the clase preamble.
        2. Report the relivant values in the config files notes section for the db. The setup
        of the database alows the developers of dram or the users to add notes about the databases
        such as versions, upload dates, changes, and such. It is good to save these so they can go
        into the project config and be saved for postarity.


        There is no need to report the values from the dram_context, those will be reported in
        annotatons.
        """
        return {
            self.name: {
                "db_type": "built_in",
                "search_type": self.search_type,
                SETUP_NOTES_TAG: self.get_setup_notes(),
            }
        }

    # def search(self, fasta:Fasta):
    #     fasta_tmp_dir = working_dir / fasta.name
    #     fasta_tmp_dir.mkdir
    #     hits = get_hits(fasta_tmp_dir, working_dir)
    #     pass

    @abstractmethod
    def search(self, fasta: Fasta):
        pass

    def get_ids(self, annotations: pd.Series) -> list:
        main_id = f"{self.name}_id"
        if main_id in annotations:
            return [annotations[main_id]]
        if main_id not in annotations:
            self.logger.debug(
                f"Expected {main_id} to be in annotations,  but it was not found"
            )
        elif not pd.isna(annotations[main_id]):
            return [annotations[main_id]]
        return []


class FastaKit(DBKit):

    name = "custom_fasta_db"
    selectable: bool = False

    def __init__(self, name: str, loc: Path):
        self.set_universals(
            name, name, {}, "Custom FASTA", "No citation for custom DBs"
        )
        self.set_args(**args)
        self.fasta_loc: Path = loc
        self.setup()
        # if none is passed from argparse then set to tuple of len 0

    def setup(self):

        temp_dir = self.working_dir / f"{self.name}_fasta_db"
        temp_dir.mkdir(exist_ok=True, parents=True)
        self.mmsdb_target = temp_dir / f"{self.name}.mmsdb"
        make_mmseqs_db(
            self.fasta_loc,
            self.mmsdb_target.as_posix(),
            logger=self.logger,
            threads=self.threads,
        )

    def load_dram_config(self):
        pass

    def search(self, query_ob: Fasta) -> pd.DataFrame:
        annotatons = do_blast_style_search(
            query_ob.mmsdb,
            self.mmsdb_target,
            self.working_dir,
            self.logger,
            self.name,
            self.bit_score_threshold,
            self.rbh_bit_score_threshold,
            self.threads,
        )
        return annotatons

    def get_descriptions(self, annotatons):
        header_dict = multigrep(
            hits[f"{self.name}_hit"],
            f"{self.mmsdb_target}_h",
            self.logger,
            "\x00",
            self.working_dir,
        )
        hits = get_basic_descriptions(annotatons, header_dict, self.name)
        return hits

    def get_settings(self) -> dict:
        return {
            self.name: {
                "db_type": CUSTOM_FASTA_DB_TYPE,
                "input_fasta": self.fasta_loc.absolute().as_posix(),
                "search_type": "blast_style",
                "bit_score": self.bit_score_threshold,
                "bit_score": self.rbh_bit_score_threshold,
            }
        }


class HmmKit(DBKit):

    name = "custom_hmm_db"
    selectable: bool = False

    def __init__(
        self,
        name: str,
        loc: Path,
        descriptions: Path,
    ):
        self.set_universals(name, name, {}, "Custom hmm", "No citation for custom DBs")
        self.hmm_loc: Path = loc
        self.descriptions: Path = descriptions
        self.fasta_loc: Path = loc
        self.setup()

    def load_dram_config(self):
        pass

    def setup(self):
        self.logger.info(f"Pre processing custom hmm database {self.name}")
        run_process(
            ["hmmpress", "-f", self.hmm_loc], self.logger
        )  # all are pressed just in case

    def search(self, query_ob: Fasta):
        self.logger.info(f"Annotating custom hmm database {self.name}")
        if query_ob.faa is None:
            raise ValueError("Fasta without associated faa error.")
        annotatons = run_hmmscan(
            genes_faa=query_ob.faa.as_posix(),
            db_loc=self.hmm_loc.as_posix(),
            db_name=self.name,
            threads=self.threads,
            output_loc=self.working_dir.as_posix(),
            formater=partial(
                generic_hmmscan_formater,
                db_name=self.name,
                hmm_info_path=self.descriptions,
                top_hit=True,
            ),
            logger=self.logger,
        )
        return annotatons

    def get_settings(self) -> dict:
        return {
            self.name: {
                "db_type": CUSTOM_HMM_DB_TYPE,
                "input_hmm": self.hmm_loc.absolute().as_posix(),
                "search_type": "hmm_style",
            }
        }
