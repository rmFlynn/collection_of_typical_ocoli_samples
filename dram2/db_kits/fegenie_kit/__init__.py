from os import path, mkdir
from glob import glob
import tarfile
import logging
import pandas as pd
from numpy import any
from functools import partial
from shutil import rmtree, copyfileobj, move
from itertools import count
from dram2.db_kits.utils import make_mmseqs_db, run_hmmscan, get_sig_row, DBKit

from dram2.utils import (
    download_file,
    run_process,
    Fasta
)


VERSION = "1.2"
NAME = "fegenie"
NAME_FORMAL = "FeGenie"
CITATION = "Garber AI, Nealson KH, Okamoto A, McAllister SM, Chan CS, Barco RA and Merino N (2020) FeGenie: A Comprehensive Tool for the Identification of Iron Genes and Iron Gene Neighborhoods in Genome and Metagenome Assemblies. Front. Microbiol. 11:37. doi: 10.3389/fmicb.2020.00037"
SETTINGS = {
    "search_databases": {
        "fegenie_hmm": {
            "location": None,
            "name": "FeGenie HMM",
            "citation": CITATION,
            "notes": "Only iron_oxidation and iron_reduction hmms are used.",
        },
    },
    "database_descriptions": {
        "fegenie_cutoffs": {"name": "FeGenie cutoffs", "citation": CITATION}
    },
    "dram_sheets": {},
}

DOWNLOAD_OPTIONS = {"fegenie_tar_gz": {"version": VERSION}}
PROCESS_OPTIONS = {"fegenie_tar_gz": {"version": VERSION}}
DRAM_SETTINGS = {}


def download(temporary, logger, version=VERSION, verbose=True):
    """
    Retrieve genie release tar.gz

    This will get a tar file from the specified FeGenie release on git hub.

    :param temporary: Usually in the output dir
    :param verbose: TODO replace with logging setting
    :returns: Path to tar
    """
    database = path.join(temporary, f"{NAME}_{version}.tar.gz")
    # Note the 'v' in the name, GitHub wants it in the tag then it just takes it out. This could be a problem
    download_file(
        f"https://github.com/Arkadiy-Garber/FeGenie/archive/refs/tags/v{version}.tar.gz",
        logger,
        database,
        verbose=verbose,
    )
    return database


def process(
    input_file, output_dir, logger, threads=1, version=VERSION, verbose=False
) -> dict:
    temp_dir = path.dirname(input_file)
    # this is the path within the tar file
    tar_paths = {
        "fegenie_hmm": [
            path.join(f"{NAME}-{version}", "iron", "iron_oxidation"),
            path.join(f"{NAME}-{version}", "iron", "iron_reduction"),
        ],
        "fegenie_cutoffs": path.join(f"{NAME}-{version}", "iron", "HMM-bitcutoffs.txt"),
    }
    final_paths = {
        "fegenie_hmm": path.join(
            output_dir, f"{NAME}-{version}", "fegenie_iron_oxidation_reduction.hmm"
        ),
        "fegenie_cutoffs": path.join(
            output_dir, f"{NAME}-{version}", "fegenie_iron_cut_offs.txt"
        ),
    }

    with tarfile.open(
        input_file,
    ) as tar:
        tar.extract(tar_paths["fegenie_cutoffs"], temp_dir)
        for info in tar.getmembers():
            tid = info.name
            if any(
                [tid.startswith(i) for i in tar_paths["fegenie_hmm"]]
            ) and tid.endswith("hmm"):
                tar.extract(tid, temp_dir)

    # move and concatanate hmm to location
    if not path.exists(path.dirname(final_paths["fegenie_hmm"])):
        mkdir(path.dirname(final_paths["fegenie_hmm"]))

    hmm_paths = [
        i
        for j in tar_paths["fegenie_hmm"]
        for i in glob(path.join(temp_dir, j, "*.hmm"))
    ]
    hmm_names = set()
    with open(final_paths["fegenie_hmm"], "wb") as wfd:
        for f in hmm_paths:
            if path.basename(f) not in hmm_names:
                hmm_names.add(path.basename(f))
                with open(f, "rb") as fd:
                    copyfileobj(fd, wfd)

    # move the cutoffs
    move(
        path.join(temp_dir, tar_paths["fegenie_cutoffs"]),
        final_paths["fegenie_cutoffs"],
    )

    # build dbs
    run_process(
        ["hmmpress", "-f", final_paths["fegenie_hmm"]], logger, verbose=verbose
    )  # all are pressed just in case
    return final_paths


# TODO check this
def sig_scores(hits: pd.DataFrame, score_db: pd.DataFrame) -> pd.DataFrame:
    """
    This is a custom sig_scores function for FeGenie, it usese soft_bitscore_cutoff
    as a bit score cutoffs, given the name I am not shure that is corect.

    Also, I use full score, is that corect?
    """
    data = pd.merge(hits, score_db, how="left", left_on="target_id", right_index=True)
    return data[data["full_score"] > data["soft_bitscore_cutoff"]]


def hmmscan_formater(
    hits: pd.DataFrame, db_name: str, hmm_info_path: str = None, top_hit: bool = True
):
    if hmm_info_path is None:
        hmm_info = None
        hits_sig = hits[hits.apply(get_sig_row, axis=1)]
    else:
        hmm_info = pd.read_csv(hmm_info_path, sep="\t", index_col=0)
        hits_sig = sig_scores(hits, hmm_info)
    if len(hits_sig) == 0:
        # if nothing significant then return nothing, don't get descriptions
        return pd.DataFrame()
    if top_hit:
        # Get the best hits
        hits_sig = hits_sig.sort_values("full_evalue").drop_duplicates(
            subset=["query_id"]
        )
    hits_df = hits_sig[["target_id", "query_id", "description"]]
    hits_df.set_index("query_id", inplace=True, drop=True)
    hits_df.rename_axis(None, inplace=True)
    hits_df.columns = [f"{db_name}_id", f"{db_name}_description"]
    return hits_df


class FeGenieKit(DBKit):
    name = NAME
    formal_name: str = NAME_FORMAL
    version: str = VERSION
    citation: str = CITATION
    max_threads:int = 2

    def setup(self):
        pass

    def load_dram_config(self):
        self.hmm = self.get_config_path('hmmdb')
        self.cutoffs = self.get_config_path('cutoffs')

    def search(self, fasta:Fasta):
        self.logger.info(f"Annotating genes with {NAME_FORMAL}.")
        return run_hmmscan(
            genes_faa=fasta.faa.as_posix(),
            db_loc=self.hmm.as_posix(),
            db_name=NAME,
            threads=self.threads,
            logger=self.logger,
            output_loc=self.working_dir.as_posix(),
            formater=partial(
                hmmscan_formater,
                db_name=NAME,
                hmm_info_path=self.cutoffs.as_posix(),
                top_hit=True,
            ),
        )


