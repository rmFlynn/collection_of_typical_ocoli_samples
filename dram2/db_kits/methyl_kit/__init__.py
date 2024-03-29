"""
Do methyl actions

TODO:
add the package data for distillate

"""

from os import path
import tarfile
from dram2.db_kits.utils import (
    do_blast_style_search,
    make_mmseqs_db,
    DBKit,
)
from dram2.utils import Fasta
import logging
import pandas as pd
from pathlib import Path

VERSION = "0.1.0"
NAME = "methyl"
NAME_FORMAL = "Methyl"
CITATION = "Methyl is a in hous db mostly make by McKayla Borton"
# target_db = "/home/projects-wrighton-2/DRAM/dram_data/dram1.4_final_06_07_22/methyl.mmsdb"
# multigrep(hits['%s_hit' % db_name], '%s_h' % target_db, '\x00', working_dir)
PROCESS_OPTIONS = {}


def process(methyl_fa, output_dir, logger:logging.Logger, threads) -> dict:
    methyl_fa_db = path.join(output_dir, "methyl.mmsdb")
    make_mmseqs_db(methyl_fa, methyl_fa_db, logger, threads)
    return {"methyl_fa_db": methyl_fa_db}


class MethylKit(DBKit):
    name = NAME
    formal_name = NAME_FORMAL
    citation: str = CITATION
    has_genome_summary: bool= True

    def get_genome_summary(self) -> Path:
        genome_summary_form = self.request_config_path("genome_summary_form")
        if genome_summary_form is None:
            raise ValueError("No methyl summary form")
        return genome_summary_form


    def setup(self):
        # somthing like
        # make_mmseqs_db("/home/Database/DRAM/methyl/methylotrophy.faa", "/home/Database/DRAM/methyl/methylotrophy.mmsdb", logging.getLogger())
        pass

    def load_dram_config(self):
        self.mmsdb = self.get_config_path("mmsdb")

    def search(self, fasta: Fasta):
        # get_custom_description = partial(get_basic_descriptions, db_name=NAME)
        tmp_dir = self.working_dir / fasta.name
        tmp_dir.mkdir()
        hits = do_blast_style_search(
            fasta.mmsdb.as_posix(),
            self.mmsdb.as_posix(),
            tmp_dir,
            self.logger,
            self.name,
            self.bit_score_threshold,
            self.rbh_bit_score_threshold,
            self.threads,
        )
        
        hits = pd.DataFrame(hits)
        return hits.rename(columns = {f"{self.name}_hit": f"{self.name}_id"})


