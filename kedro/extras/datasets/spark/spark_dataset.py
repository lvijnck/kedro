# Copyright 2021 QuantumBlack Visual Analytics Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND
# NONINFRINGEMENT. IN NO EVENT WILL THE LICENSOR OR OTHER CONTRIBUTORS
# BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# The QuantumBlack Visual Analytics Limited ("QuantumBlack") name and logo
# (either separately or in combination, "QuantumBlack Trademarks") are
# trademarks of QuantumBlack. The License does not grant you any right or
# license to the QuantumBlack Trademarks. You may not use the QuantumBlack
# Trademarks or any confusingly similar mark as a trademark for your product,
# or use the QuantumBlack Trademarks in any other manner that might cause
# confusion in the marketplace, including but not limited to in advertising,
# on websites, or on software.
#
# See the License for the specific language governing permissions and
# limitations under the License.

"""``AbstractDataSet`` implementation to access Spark dataframes using
``pyspark``
"""

import json
from copy import deepcopy
from fnmatch import fnmatch
from functools import partial
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from warnings import warn

import fsspec
from hdfs import HdfsError, InsecureClient
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql.types import StructType
from pyspark.sql.utils import AnalysisException
from s3fs import S3FileSystem

from kedro.io.core import (
    AbstractVersionedDataSet,
    Version,
    get_filepath_str,
    get_protocol_and_path,
)


def _parse_glob_pattern(pattern: str) -> str:
    special = ("*", "?", "[")
    clean = []
    for part in pattern.split("/"):
        if any(char in part for char in special):
            break
        clean.append(part)
    return "/".join(clean)


def _split_filepath(filepath: str) -> Tuple[str, str]:
    split_ = filepath.split("://", 1)
    if len(split_) == 2:
        return split_[0] + "://", split_[1]
    return "", split_[0]


def _strip_dbfs_prefix(path: str, prefix: str = "/dbfs") -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path


def _dbfs_glob(pattern: str, dbutils: Any) -> List[str]:
    """Perform a custom glob search in DBFS using the provided pattern.
    It is assumed that version paths are managed by Kedro only.

    Args:
        pattern: Glob pattern to search for.
        dbutils: dbutils instance to operate with DBFS.

    Returns:
            List of DBFS paths prefixed with '/dbfs' that satisfy the glob pattern.
    """
    pattern = _strip_dbfs_prefix(pattern)
    prefix = _parse_glob_pattern(pattern)
    matched = set()
    filename = pattern.split("/")[-1]

    for file_info in dbutils.fs.ls(prefix):
        if file_info.isDir():
            path = str(
                PurePosixPath(_strip_dbfs_prefix(file_info.path, "dbfs:")) / filename
            )
            if fnmatch(path, pattern):
                path = "/dbfs" + path
                matched.add(path)
    return sorted(matched)


def _get_dbutils(spark: SparkSession) -> Optional[Any]:
    """Get the instance of 'dbutils' or None if the one could not be found."""
    dbutils = globals().get("dbutils")
    if dbutils:
        return dbutils

    try:
        from pyspark.dbutils import DBUtils  # pylint: disable=import-outside-toplevel

        dbutils = DBUtils(spark)
    except ImportError:
        try:
            import IPython  # pylint: disable=import-outside-toplevel
        except ImportError:
            pass
        else:
            ipython = IPython.get_ipython()
            dbutils = ipython.user_ns.get("dbutils") if ipython else None

    return dbutils


def _dbfs_exists(pattern: str, dbutils: Any) -> bool:
    """Perform an `ls` list operation in DBFS using the provided pattern.
    It is assumed that version paths are managed by Kedro.
    Broad `Exception` is present due to `dbutils.fs.ExecutionError` that
    cannot be imported directly.
    Args:
        pattern: Filepath to search for.
        dbutils: dbutils instance to operate with DBFS.
    Returns:
        Boolean value if filepath exists.
    """
    pattern = _strip_dbfs_prefix(pattern)
    file = _parse_glob_pattern(pattern)
    try:
        dbutils.fs.ls(file)
        return True
    except Exception:  # pylint: disable=broad-except
        return False


class KedroHdfsInsecureClient(InsecureClient):
    """Subclasses ``hdfs.InsecureClient`` and implements ``hdfs_exists``
    and ``hdfs_glob`` methods required by ``SparkDataSet``"""

    def hdfs_exists(self, hdfs_path: str) -> bool:
        """Determines whether given ``hdfs_path`` exists in HDFS.

        Args:
            hdfs_path: Path to check.

        Returns:
            True if ``hdfs_path`` exists in HDFS, False otherwise.
        """
        return bool(self.status(hdfs_path, strict=False))

    def hdfs_glob(self, pattern: str) -> List[str]:
        """Perform a glob search in HDFS using the provided pattern.

        Args:
            pattern: Glob pattern to search for.

        Returns:
            List of HDFS paths that satisfy the glob pattern.
        """
        prefix = _parse_glob_pattern(pattern) or "/"
        matched = set()
        try:
            for dpath, _, fnames in self.walk(prefix):
                if fnmatch(dpath, pattern):
                    matched.add(dpath)
                matched |= {
                    f"{dpath}/{fname}"
                    for fname in fnames
                    if fnmatch(f"{dpath}/{fname}", pattern)
                }
        except HdfsError:  # pragma: no cover
            # HdfsError is raised by `self.walk()` if prefix does not exist in HDFS.
            # Ignore and return an empty list.
            pass
        return sorted(matched)


class SparkDataSet(AbstractVersionedDataSet):
    """``SparkDataSet`` loads and saves Spark dataframes.
    Example:
    ::

        >>> from pyspark.sql import SparkSession
        >>> from pyspark.sql.types import (StructField, StringType,
        >>>                                IntegerType, StructType)
        >>>
        >>> from kedro.extras.datasets.spark import SparkDataSet
        >>>
        >>> schema = StructType([StructField("name", StringType(), True),
        >>>                      StructField("age", IntegerType(), True)])
        >>>
        >>> data = [('Alex', 31), ('Bob', 12), ('Clarke', 65), ('Dave', 29)]
        >>>
        >>> spark_df = SparkSession.builder.getOrCreate()\
        >>>                        .createDataFrame(data, schema)
        >>>
        >>> data_set = SparkDataSet(filepath="test_data")
        >>> data_set.save(spark_df)
        >>> reloaded = data_set.load()
        >>>
        >>> reloaded.take(4)
    """

    # this dataset cannot be used with ``ParallelRunner``,
    # therefore it has the attribute ``_SINGLE_PROCESS = True``
    # for parallelism within a Spark pipeline please consider
    # ``ThreadRunner`` instead
    _SINGLE_PROCESS = True
    DEFAULT_LOAD_ARGS = {}  # type: Dict[str, Any]
    DEFAULT_SAVE_ARGS = {}  # type: Dict[str, Any]

    def __init__(  # pylint: disable=too-many-arguments
        self,
        filepath: str,
        file_format: str = "parquet",
        load_args: Dict[str, Any] = None,
        save_args: Dict[str, Any] = None,
        version: Version = None,
        credentials: Dict[str, Any] = None,
    ) -> None:
        """Creates a new instance of ``SparkDataSet``.

        Args:
            filepath: Filepath in POSIX format to a Spark dataframe. When using Databricks
                and working with data written to mount path points,
                specify ``filepath``s for (versioned) ``SparkDataSet``s
                starting with ``/dbfs/mnt``.
            file_format: File format used during load and save
                operations. These are formats supported by the running
                SparkContext include parquet, csv. For a list of supported
                formats please refer to Apache Spark documentation at
                https://spark.apache.org/docs/latest/sql-programming-guide.html
            load_args: Load args passed to Spark DataFrameReader load method.
                It is dependent on the selected file format. You can find
                a list of read options for each supported format
                in Spark DataFrame read documentation:
                https://spark.apache.org/docs/latest/api/python/reference/api/pyspark.sql.DataFrame.html
            save_args: Save args passed to Spark DataFrame write options.
                Similar to load_args this is dependent on the selected file
                format. You can pass ``mode`` and ``partitionBy`` to specify
                your overwrite mode and partitioning respectively. You can find
                a list of options for each format in Spark DataFrame
                write documentation:
                https://spark.apache.org/docs/latest/api/python/reference/api/pyspark.sql.DataFrame.html
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            credentials: Credentials to access the S3 bucket, such as
                ``key``, ``secret``, if ``filepath`` prefix is ``s3a://`` or ``s3n://``.
                Optional keyword arguments passed to ``hdfs.client.InsecureClient``
                if ``filepath`` prefix is ``hdfs://``. Ignored otherwise.
        """
        credentials = deepcopy(credentials) or {}
        fs_prefix, filepath = _split_filepath(filepath)
        exists_function = None
        glob_function = None

        if fs_prefix in ("s3a://", "s3n://"):
            if fs_prefix == "s3n://":
                warn(
                    "`s3n` filesystem has now been deprecated by Spark, "
                    "please consider switching to `s3a`",
                    DeprecationWarning,
                )
            _s3 = S3FileSystem(**credentials)
            exists_function = _s3.exists
            glob_function = partial(_s3.glob, refresh=True)
            path = PurePosixPath(filepath)

        elif fs_prefix == "hdfs://" and version:
            warn(
                f"HDFS filesystem support for versioned {self.__class__.__name__} is "
                f"in beta and uses `hdfs.client.InsecureClient`, please use with "
                f"caution"
            )

            # default namenode address
            credentials.setdefault("url", "http://localhost:9870")
            credentials.setdefault("user", "hadoop")

            _hdfs_client = KedroHdfsInsecureClient(**credentials)
            exists_function = _hdfs_client.hdfs_exists
            glob_function = _hdfs_client.hdfs_glob  # type: ignore
            path = PurePosixPath(filepath)

        else:
            path = PurePosixPath(filepath)

            if filepath.startswith("/dbfs"):
                dbutils = _get_dbutils(self._get_spark())
                if dbutils:
                    glob_function = partial(_dbfs_glob, dbutils=dbutils)
                    exists_function = partial(_dbfs_exists, dbutils=dbutils)

        super().__init__(
            filepath=path,
            version=version,
            exists_function=exists_function,
            glob_function=glob_function,
        )

        # Handle default load and save arguments
        self._load_args = deepcopy(self.DEFAULT_LOAD_ARGS)
        if load_args is not None:
            self._load_args.update(load_args)
        self._save_args = deepcopy(self.DEFAULT_SAVE_ARGS)
        if save_args is not None:
            self._save_args.update(save_args)

        # Handle schema
        self._schema = self._load_schema(self._load_args.pop("schema_json_path", None))
        self._file_format = file_format
        self._fs_prefix = fs_prefix

    @staticmethod
    def _load_schema(schema_json_path: str) -> Optional[StructType]:
        if schema_json_path is None:
            return None

        # TODO Limit protocols to file only?
        # TODO What about files in HDFS?
        # TODO What about credentials, e.g., schema stored in separate GCS bucket?
        protocol, schema_path = get_protocol_and_path(schema_json_path)
        file_system = fsspec.filesystem(protocol)
        pure_posix_path = PurePosixPath(schema_path)
        load_path = get_filepath_str(pure_posix_path, protocol)

        # Open schema file
        with file_system.open(load_path) as fs_file:

            # TODO lazy load schema when loading dataframe?
            # TODO Support other schema input formats?
            # TODO What if file is in the wrong format?
            return StructType.fromJson(json.loads(fs_file.read()))

    def _describe(self) -> Dict[str, Any]:
        return dict(
            filepath=self._fs_prefix + str(self._filepath),
            file_format=self._file_format,
            load_args=self._load_args,
            save_args=self._save_args,
            version=self._version,
        )

    @staticmethod
    def _get_spark():
        return SparkSession.builder.getOrCreate()

    def _load(self) -> DataFrame:
        load_path = _strip_dbfs_prefix(self._fs_prefix + str(self._get_load_path()))
        read_obj = self._get_spark().read

        # Pass schema if defined
        if self._schema:
            read_obj = read_obj.schema(self._schema)

        return read_obj.load(load_path, self._file_format, **self._load_args)

    def _save(self, data: DataFrame) -> None:
        save_path = _strip_dbfs_prefix(self._fs_prefix + str(self._get_save_path()))
        data.write.save(save_path, self._file_format, **self._save_args)

    def _exists(self) -> bool:
        load_path = _strip_dbfs_prefix(self._fs_prefix + str(self._get_load_path()))

        try:
            self._get_spark().read.load(load_path, self._file_format)
        except AnalysisException as exception:
            if exception.desc.startswith("Path does not exist:"):
                return False
            raise
        return True
