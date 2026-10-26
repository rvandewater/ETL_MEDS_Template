# Mapping an unfamiliar clinical dataset to MEDS

Use this checklist when adapting a configuration-only MEDS-Extract ETL.

## 1. Establish facts before mapping

- Read the dataset's data dictionary **and** release documentation. The dictionary identifies columns; release notes often define time origins, deidentification, and table grain.
- Inspect real headers, row counts, nulls, dtypes, value ranges, and join-key coverage. Do not infer them from a reference ETL.
- Treat each source table independently. A copied table name, timestamp format, vocabulary map, or identifier is a bug until the target data documents it.
- Keep raw data out of commits. A synthetic input is the right test fixture for restricted or large releases.

## 2. Write a source-to-MEDS mapping

For every source table, record:

| Source fact                           | MEDS decision                                                                      |
| ------------------------------------- | ---------------------------------------------------------------------------------- |
| Patient key / encounter key           | `subject_id` expression and any required join                                      |
| Event time and its origin             | timestamp expression, including an explicit pseudotime anchor for relative offsets |
| Observation                           | `code`, `numeric_value`, `text_value`, and only documented extension columns       |
| Vocabulary / label                    | metadata description or parent code, only when a documented map exists             |
| Missing, invalid, or ambiguous values | whether to drop, retain as text, or use a documented sentinel                      |

Never invent a clinical mapping. Prefer a readable local code (for example `LAB//{category}//{item}//{unit}`) to an unsupported standard-vocabulary parent.

## 3. Relative timestamps need a shared anchor

MEDS requires dates or datetimes, while deidentified datasets often store offsets. Convert an offset in hours with a fixed, documented anchor:

```yaml
time: 1970-01-01 @ 00:00 + $offset::hours
```

The anchor preserves intervals and ordering without implying a real admission date. Use the same anchor everywhere. Guard source values known to be invalid or outside the documented encounter window; a bad offset must become a null timestamp and be dropped, not become a plausible-looking event decades away.

## 4. Keep identifier joins explicit

A source may use a patient key in one table and an admission key in another. Declare the join in the table that lacks the patient key, pull only the needed key or encounter boundary, and test that the join covers all intended rows. Do not hash encounter identifiers into subject identifiers when the dataset documents a patient identifier.

## 5. Map the data, not a preferred schema

- Encode demographics and baseline categories as static or admission-time events only when their semantics are documented.
- Encode diagnoses, medications, labs, transfers, and nursing measurements from their own tables.
- Keep nonnumeric clinical values in `text_value`; use a lenient numeric cast only when the source column is genuinely mixed-format.
- Wide nursing charts need one explicit event per documented measurement. Ignore fields documented as nonclinical or known technical duplicates unless there is a clear requested use.
- Follow-up forms can be represented as timed outcome/questionnaire events, but only at their documented follow-up time.

## 6. Validate twice

1. **Synthetic smoke test:** Generate raw files directly from the MESSY config with `MESSY-synth`, then run `meds-extract-run` with downloads disabled. This checks parsing, required columns, joins, expressions, and MEDS artifacts without real data.
2. **Local real-data validation:** When authorized source files are available, run the same command with `input_dir`. Inspect emitted data and metadata for row presence, valid timestamps, expected dataset name, and no accidental source-specific leftovers.

A successful process exit is insufficient: assert that `data/**/*.parquet`, `metadata/dataset.json`, `metadata/codes.parquet`, and `metadata/subject_splits.parquet` exist.

## 7. Keep the package coherent

Rename the package, entry point, config path, README usage, and CI synthetic command together. Do not modify the reference ETL; create a separate target package. The only shared project changes should be the registration and checks needed to execute the new ETL.
