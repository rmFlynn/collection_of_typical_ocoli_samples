"""
Context Mannager
________________

store the context for dram.

Note:

config for dram
---------------

A file path should have the key 'location' and that path can be absolute or
relitive. If the paths are absolute then they are pointing to the exact location
of the folder. Absoule paths for every single folder are hard to work with and
all but imposible to move, most people will use relitve paths. If the paths are
relative they are relative to the value of  the dram data folder where it
will be asuemed all dram data is stored. The dram data folder is specified with
the key 'dram_data_folder' and this path can also be relative or absolute. If it
is absolute it will point to the exact location of the dram data if it is
relative it will point to the location of the of the data folder with respect to
the folder that contains the config file its self. So for example if the sead
folder
was "dram_data_folder: ./" the data must be stored in the same folder as the
config.

"""
import collections
import logging
from typing import Optional
from datetime import datetime
from pathlib import Path

import yaml
import click


from dram2.db_kits.utils import DRAM_DATAFOLDER_TAG, FILE_LOCATION_TAG

PROJECT_CONFIG_YAML_NAME = "project_config.yaml"
USER_CONFIG = Path.home() /  ".config" / "dram_config.yaml"
GLOBAL_CONFIG = Path("/etc") / "dram_config.yaml"
DEFAULT_KEEP_TMP = False

__version__ = "2.0.0"

def get_config_path(logger:logging.Logger, custom_path: Optional[Path] = None) -> Path:
    if custom_path is not None and custom_path.exists:
        logger.debug(f"Loading custom config from: {custom_path.as_posix()}")
        return custom_path
    if USER_CONFIG.exists():
        logger.debug(f"Loading user config from: {USER_CONFIG.as_posix()}")
        return USER_CONFIG
    if GLOBAL_CONFIG.exists():
        logger.debug(f"Loading global config from: {GLOBAL_CONFIG.as_posix()}")
        return GLOBAL_CONFIG

    serched_paths = ", ".join(
        {i for i in [custom_path, USER_CONFIG, GLOBAL_CONFIG] if i is not None}
    )
    raise ValueError(
        f"There is not config file found, DRAM looked at the falowing paths {serched_paths}"
    )


def get_new_config_path(
    custom_path: Optional[Path] = None, conf_type: bool = False
) -> Path:
    """
    If the user gives a path put it there, else if the conf_type is global then put it in the global position if it is local or none then put it in local/user.

    It can be eddited when python3.10 is more Common into a match statment
    """
    if custom_path is not None:
        return custom_path
    if conf_type == "global":
        return GLOBAL_CONFIG
    else:
        return USER_CONFIG

def get_time_stamp_id(prefix:Optional[str]) -> str:
    timestamp_id: str = datetime.now().strftime("%Y%m%d%H%M%S")
    if prefix is None:
        return timestamp_id
    return f"{prefix}_{timestamp_id}"

class DramContext(object):

    working_dir: Optional[Path]
    dram_config_path: Path

    def __init__(
        self,
        cores: int,
        db_path: Optional[Path],
        config_file: Optional[Path],
        log_file_path: Optional[Path],
        output_dir: Optional[Path],
        keep_tmp: bool,
        verbose=int,
    ):
        self.cores = cores
        self.db_path = db_path
        self.custom_config_file = config_file
        self.log_file_path = log_file_path
        self.output_dir = output_dir
        # self.force: bool = force
        self.verbose = verbose
        self.keep_tmp: bool = keep_tmp
        self.project_config: Optional[dict] = None
        # Make a working_dir that may be deleted
        # self.working_dir.mkdir(exist_ok=True)

    # def get_working_dir(self, Optional[prefix] = None):
    #     output_dir = self.get_output_dir()
    #     if prefix is None:
    #         self.working_dir = output_dir / "working_dir"
    #     else:
    #         timestamp_id: str = datetime.now().strftime("%Y%m%d%H%M%S")
    #         self.working_dir = output_dir / f"prefix_{timestamp_id}"
    #     self.working_dir.mkdir(exist_ok=True)
    #     return self.working_dir

    def get_output_dir(self) -> Path:
        if self.output_dir is None:
            raise ValueError(
                "You need to set an output directory or you can't use dram use `dram2 --help` and revue the docs."
            )
        if not self.output_dir.exists():
            self.output_dir.mkdir()
        # elif not self.force:
        #     raise ValueError(
        #         "The output_dir already exists! try using the -f flag to overwrite"
        #         )
        return self.output_dir

    def get_project_config(self) -> dict:
        output_dir = self.get_output_dir()
        project_config_path = output_dir / PROJECT_CONFIG_YAML_NAME
        self.project_config = {}
        if project_config_path.exists():
            with open(project_config_path, "r") as pcf:
                saved_config = yaml.safe_load(pcf)
                if saved_config is not None:
                    self.project_config.update(saved_config)
        return self.project_config

    def set_project_config(self, project_config: dict, write_config: bool = True):
        output_dir = self.get_output_dir()
        project_config_path = output_dir / PROJECT_CONFIG_YAML_NAME
        self.project_config = project_config
        if write_config:
            with open(project_config_path, "w") as pcf:
                yaml.safe_dump(self.project_config, pcf)

    def get_logger(self):
        """
        Setup the Logger
        ________________

        Logging with a dedicated logger is the best way to log in
        python and that is how we do it. Here we setup that logger
        and provide it with the necciary context such as the output
        file, format, and the level of verbosity aka level.

        This is all basicaly pulled strate from the docs.
        
        this consumes log_file_path, and verbosity it returns a fully
        formed logger.

        This function coverts the verbosity integer to the log level the maping is:

            0 = CRITICAL 
            1 = ERROR 
            2 = WARNING 
            3 = INFO 
            4 = DEBUG 
            5 >= NOTSET 
        Note that 5 is the max, logg levels past 5 will get the same result as 5

        Docs: https://docs.python.org/3/library/logging.html
        
        """ 
        logger = logging.getLogger("dram2_log")

        # conver verbosity to log levels
        match self.verbose:
            case 1:
                level = logging.CRITICAL # numaric level = 50
            case 2:
                level = logging.ERROR # numaric level = 40
            case 3:
                level = logging.WARNING # numaric level = 30
            case 4:
                level = logging.INFO # numaric level = 20
            case 5:
                level = logging.DEBUG # numaric level = 10
            case _:
                level = logging.DEBUG# numaric level = 0

        if self.log_file_path is None:
            output_dir = self.get_output_dir()
            log_file_path = output_dir / "dram2.log"
        else:
            log_file_path = self.log_file_path
        formatter = logging.Formatter("%(asctime)s - %(message)s")
        # create console handler
        ch = logging.StreamHandler()
        ch.setLevel(level)
        # create formatter and add it to the handlers
        ch.setFormatter(formatter)
        fh = logging.FileHandler(log_file_path)
        # level less sevire than this level will be ingnored
        fh.setLevel(level)
        fh.setFormatter(formatter)
        # add the handlers to the logger
        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.setLevel(level)
        logger.info(f"The log file is created at {log_file_path}")
        return logger

    def get_dram_config(self, logger) -> dict:
        """
        Load the DRAM config file and find the DRAM data folder. Note that this
        dose not fail if there is no data folder specified. It dose not
        resolve relitve paths, it dose not error if files don't exist. All that
        is done by the checking step of the db_kit using the file.
        ___
        :returns: A config dictionary with the path resuolved
        :raises ValueError: When an error in the config file is found
        """
        dram_config_path = get_config_path(logger, self.custom_config_file)
        with open(dram_config_path, "r") as conf:
            config = yaml.safe_load(conf)
        data_folder = config.get(DRAM_DATAFOLDER_TAG)
        if data_folder is None:
            logger.warn(
                "The config passed to DRAM dose not contain the key"
                f" {DRAM_DATAFOLDER_TAG}. That key would point "
                "to an existing folder of dram data ether relive to the folder "
                "containg the config file or the absolute path. Without it you "
                "must use absolute paths to all files requierd by dram. You "
                "have now been warned that this may cause DRAM to fail."
            )
            data_folder_path = None
            config[DRAM_DATAFOLDER_TAG] = None
            return config
        data_folder_path = Path(data_folder)
        if not data_folder_path.is_absolute():
            data_folder_path = (dram_config_path.parent / data_folder_path).absolute()
        config[DRAM_DATAFOLDER_TAG] = data_folder_path
        return config

    def set_dram_config(
        self,
        config: dict,
        custom_path: Optional[Path] = None,
        type: Optional[str] = None,
    ):
        pass


class OrderedGroup(click.Group):
    def __init__(self, name=None, commands=None, **attrs):
        super(OrderedGroup, self).__init__(name, commands, **attrs)
        #: the registered subcommands by their exported names.
        self.commands = commands or collections.OrderedDict()

    def list_commands(self, ctx):
        return self.commands
