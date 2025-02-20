"""Performs pre-MEDS data wrangling for INSERT DATASET NAME HERE."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import polars as pl
from loguru import logger
from MEDS_transforms.utils import get_shard_prefix, write_lazyframe
from omegaconf import DictConfig, OmegaConf

from ETL_MEDS import dataset_info, premeds_cfg

# Name of the dataset
DATASET_NAME = dataset_info.dataset_name
# Column name for admission ID associated with this particular admission
ADMISSION_ID = premeds_cfg.admission_id
# Column name for subject ID
SUBJECT_ID = premeds_cfg.subject_id
# List of file extensions to be processed
DATA_FILE_EXTENSIONS = premeds_cfg.raw_data_extensions
# List of tables to be ignored during processing
IGNORE_TABLES = []


def get_patient_link(df: pl.LazyFrame) -> (pl.LazyFrame, pl.LazyFrame):
    """
    Process the operations table to get the patient table and the link table.

    As dataset may store only offset times, note here that we add a CONSTANT TIME ACROSS ALL PATIENTS for the
    true timestamp of their health system admission.

    The output of this process is ultimately converted to events via the `patient` key in the
    `configs/event_configs.yaml` file.
    """
    admission_time = pl.datetime()
    age_in_years = pl.col()
    age_in_days = age_in_years * 365.25

    pseudo_date_of_birth = admission_time - pl.duration(days=age_in_days)
    pseudo_date_of_death = admission_time + pl.duration(seconds=pl.col())

    return (
        df.sort(by="AdmissionYear")
        .group_by(SUBJECT_ID)
        .first()
        .select(
            SUBJECT_ID,
            pseudo_date_of_birth.alias("date_of_birth"),
            admission_time.alias("first_admitted_at_time"),
            pseudo_date_of_death.alias("date_of_death"),
        ),
        df.select(SUBJECT_ID, ADMISSION_ID),
    )


def join_and_get_pseudotime_fntr(
    table_name: str,
    offset_col: str | list[str],
    pseudotime_col: str | list[str],
    reference_col: str | list[str] | None = None,
    output_data_cols: list[str] | None = None,
    warning_items: list[str] | None = None,
) -> Callable[[pl.LazyFrame, pl.LazyFrame], pl.LazyFrame]:
    """Returns a function that joins a dataframe to the `patient` table and adds pseudotimes.
    Also raises specified warning strings via the logger for uncertain columns.
    All args except `table_name` are taken from the table_preprocessors.yaml.
    Args:
        table_name: name of the INSPIRE table that should be joined
        offset_col: list of all columns that contain time offsets since the patient's first admission
        pseudotime_col: list of all timestamp columns derived from `offset_col` and the linked `patient`
            table
        output_data_cols: list of all data columns included in the output
        warning_items: any warnings noted in the table_preprocessors.yaml

    Returns:
        Function that expects the raw data stored in the `table_name` table and the joined output of the
        `process_patient_and_admissions` function. Both inputs are expected to be `pl.DataFrame`s.

    Examples:
        >>> func = join_and_get_pseudotime_fntr(
        ...     "operations",
        ...     ["admission_time", "icuin_time", "icuout_time", "orin_time", "orout_time",
        ...      "opstart_time", "opend_time", "discharge_time", "anstart_time", "anend_time",
        ...      "cpbon_time", "cpboff_time", "inhosp_death_time", "allcause_death_time", "opdate"],
        ...     ["admission_time", "icuin_time", "icuout_time", "orin_time", "orout_time",
        ...      "opstart_time", "opend_time", "discharge_time", "anstart_time", "anend_time",
        ...      "cpbon_time", "cpboff_time", "inhosp_death_time", "allcause_death_time", "opdate"],
        ...     ["subject_id", "op_id", "age", "antype", "sex", "weight", "height", "race", "asa",
        ...      "case_id", "hadm_id", "department", "emop", "icd10_pcs", "date_of_birth",
        ...      "date_of_death"],
        ...     ["How should we deal with op_id and subject_id?"]
        ... )
        >>> df = load_raw_file(Path("tests/operations_synthetic.csv"))
        >>> raw_admissions_df = load_raw_file(Path("tests/operations_synthetic.csv"))
        >>> patient_df, link_df = get_patient_link(raw_admissions_df)
        >>> references_df = load_raw_file(Path("tests/d_references.csv"))
        >>> processed_df = func(df, patient_df, references_df)
        >>> type(processed_df)
        >>> <class 'polars.lazyframe.frame.LazyFrame'>
    """

    if output_data_cols is None:
        output_data_cols = []

    if reference_col is None:
        reference_col = []

    if isinstance(offset_col, str):
        offset_col = [offset_col]
    if isinstance(pseudotime_col, str):
        pseudotime_col = [pseudotime_col]
    if isinstance(reference_col, str):
        reference_col = [reference_col]

    if len(offset_col) != len(pseudotime_col):
        raise ValueError(
            "There must be the same number of `offset_col`s and `pseudotime_col`s specified. Got "
            f"{len(offset_col)} and {len(pseudotime_col)}, respectively."
        )
    if set(offset_col) & set(output_data_cols) or set(pseudotime_col) & set(output_data_cols):
        raise ValueError(
            "There is an overlap between `offset_col` or `pseudotime_col` and `output_data_cols`: "
            f"{set(offset_col) & set(output_data_cols) | set(pseudotime_col) & set(output_data_cols)}"
        )

    def fn(df: pl.LazyFrame, patient_df: pl.LazyFrame, references_df: pl.LazyFrame) -> pl.LazyFrame:
        f"""Takes the {table_name} table and converts it to a form that includes pseudo-timestamps.

        The output of this process is ultimately converted to events via the `{table_name}` key in the
        `configs/event_configs.yaml` file.

        Args:
            df: The raw {table_name} data.
            patient_df: The processed patient data.

        Returns:
            The processed {table_name} data.
        """
        pseudotimes = [
            (pl.col("first_admitted_at_time") + pl.duration(seconds=pl.col(offset))).alias(pseudotime)
            for pseudotime, offset in zip(pseudotime_col, offset_col)
        ]
        if warning_items:
            warning_lines = [
                f"NOT SURE ABOUT THE FOLLOWING for {table_name} table. Check with the {DATASET_NAME} team:",
                *(f"  - {item}" for item in warning_items),
            ]
            logger.warning("\n".join(warning_lines))
        logger.info(f"Joining {table_name} to patient table...")
        logger.info(df.collect_schema())
        # Join the patient table to the data table, INSPIRE only has subject_id as key
        joined = df.join(patient_df.lazy(), on=ADMISSION_ID, how="inner")
        if len(reference_col) > 0:
            joined = joined.join(references_df, left_on=reference_col, right_on="REPLACE_ME")
        return joined.select(SUBJECT_ID, ADMISSION_ID, *pseudotimes, *output_data_cols)

    return fn


def load_raw_file(fp: Path) -> pl.LazyFrame:
    """Loads a raw file into a Polars DataFrame."""
    return pl.scan_csv(fp)


def main(cfg: DictConfig) -> None:
    """Performs pre-MEDS data wrangling for INSERT DATASET NAME HERE."""

    logger.info(f"Loading table preprocessors from {PRE_MEDS_CFG}...")
    preprocessors = OmegaConf.load(PRE_MEDS_CFG)
    functions = {}

    input_dir = Path(cfg.raw_input_dir)
    MEDS_input_dir = Path(cfg.root_output_dir) / "pre_MEDS"
    MEDS_input_dir.mkdir(parents=True, exist_ok=True)

    done_fp = MEDS_input_dir / ".done"
    if done_fp.is_file() and not cfg.do_overwrite:
        logger.info(
            f"Pre-MEDS transformation already complete as {done_fp} exists and "
            f"do_overwrite={cfg.do_overwrite}. Returning."
        )
        exit(0)

    all_fps = []
    for ext in DATA_FILE_EXTENSIONS:
        all_fps.extend(input_dir.rglob(f"*/{ext}"))
        all_fps.extend(input_dir.rglob(f"{ext}"))

    for table_name, preprocessor_cfg in preprocessors.items():
        logger.info(f"  Adding preprocessor for {table_name}:\n{OmegaConf.to_yaml(preprocessor_cfg)}")
        functions[table_name] = join_and_get_pseudotime_fntr(table_name=table_name, **preprocessor_cfg)

    unused_tables = {}
    patient_out_fp = MEDS_input_dir / ""
    references_out_fp = MEDS_input_dir / ""
    link_out_fp = MEDS_input_dir / ""

    if patient_out_fp.is_file() and link_out_fp.is_file():
        logger.info(f"Reloading processed patient df from {str(patient_out_fp.resolve())}")
        patient_df = pl.read_parquet(patient_out_fp, use_pyarrow=True)
        link_df = pl.read_parquet(link_out_fp, use_pyarrow=True)
    else:
        logger.info("Processing patient table...")

        admissions_fp = Path("")
        logger.info(f"Loading {str(admissions_fp.resolve())}...")
        raw_admissions_df = load_raw_file(admissions_fp)

        patient_df, link_df = get_patient_link(raw_admissions_df)
        write_lazyframe(patient_df, patient_out_fp)
        write_lazyframe(link_df, link_out_fp)

    if references_out_fp.is_file():
        logger.info(f"Reloading processed references df from {str(references_out_fp.resolve())}")
        references_df = pl.read_parquet(references_out_fp, use_pyarrow=True).lazy()
    else:
        logger.info("Processing references table first...")
        references_fp = Path("")
        logger.info(f"Loading {str(references_fp.resolve())}...")
        references_df = load_raw_file(references_fp)
        write_lazyframe(references_df, references_out_fp)

    patient_df = patient_df.join(link_df, on=SUBJECT_ID)

    for in_fp in all_fps:
        pfx = get_shard_prefix(input_dir, in_fp)
        if pfx in unused_tables:
            logger.warning(f"Skipping {pfx} as it is not supported in this pipeline.")
            continue
        elif pfx not in functions:
            logger.warning(f"No function needed for {pfx}. For {DATASET_NAME}, THIS IS UNEXPECTED")
            continue

        out_fp = MEDS_input_dir / f"{pfx}.parquet"

        if out_fp.is_file():  # and not pfx == "data_float_h" :
            logger.info(f"Done with {pfx}. Continuing")
            continue

        out_fp.parent.mkdir(parents=True, exist_ok=True)

        st = datetime.now()
        logger.info(f"Processing {pfx}...")
        df = load_raw_file(in_fp)

        fn = functions[pfx]
        processed_df = fn(df, patient_df, references_df)

        # Sink throws errors, so we use collect instead
        logger.info(
            f"patient_df schema: {patient_df.collect_schema()}, "
            f"processed_df schema: {processed_df.collect_schema()}"
        )
        processed_df.sink_parquet(out_fp)
        logger.info(f"Processed and wrote to {str(out_fp.resolve())} in {datetime.now() - st}")

    logger.info(f"Done! All dataframes processed and written to {str(MEDS_input_dir.resolve())}")
    return
