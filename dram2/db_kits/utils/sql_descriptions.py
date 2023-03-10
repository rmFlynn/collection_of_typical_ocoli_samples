from os import path, remove, getenv
from pkg_resources import resource_filename
import json
import gzip
import logging
from shutil import copy2
import warnings
from datetime import datetime
from functools import partial
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from abc import ABC, abstractmethod

from pathlib import Path
import pandas as pd
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base

BASE = declarative_base()


def create_description_db(db_loc):
    engine = create_engine("sqlite:///%s" % db_loc)
    BASE.metadata.create_all(engine)


def divide_chunks(l, n):
    # looping till length l
    for i in range(0, len(l), n):
        yield l[i : i + n]


class SQLDescriptions():
    def __init__(
        self, description_loc: Path, logger: logging.Logger, description_class, db_name:str
    ):
        self.logger = logger
        self.description_class = description_class
        self.description_loc = description_loc
        self.db_name = db_name

    def setup(self):
        pass


    def start_db_session(self):
        engine = create_engine(f"sqlite:///{self.description_loc}")
        db_session = sessionmaker(bind=engine)
        self.session = db_session()

    # functions for adding descriptions to tables
    def add_descriptions_to_database(self, description_list, clear_table=True):
        if clear_table:
            self.session.query(self.description_class).delete()
        # TODO: try batching
        self.session.bulk_save_objects(
            [self.description_class(**i) for i in description_list]
        )
        self.session.commit()
        self.session.expunge_all()

    # functions for getting descriptions from tables
    def get_description(self, annotation_id, return_ob=False):
        return (
            self.session.query(self.description_class)
            .filter_by(id=annotation_id)
            .one()
            .description
        )

    def get_descriptions(self, ids: list|pd.Series, description_name="description", none_descriptors: Optional[set] = None):
        """
        Pull The descriptions from SQLDB 
        ________________________________

        the dram database
        """
        if not isinstance(ids, list):
            ids: list = ids.dropna().values
        self.start_db_session()
        descriptions = [ des for chunk in divide_chunks(list(ids), 499) for des in self.session.query(self.description_class).filter(self.description_class.id.in_(chunk)).all() ]
        self.session.close()
        if len(descriptions) == 0: 
            for i in list(ids):
                if none_descriptors is None or i not in none_descriptors:
                    self.logger.warn(
                        "No descriptions were found for your id's. Does the id \"%s\" look like an id from %s"
                        % (i, self.db_name)
                    )
                break

        return {i.id: i.__dict__[description_name] for i in descriptions}

    # TODO: Make option to build on description database that already exists?
    def populate_description_db(self, output_loc: Path, select_db: Path):
        pass
