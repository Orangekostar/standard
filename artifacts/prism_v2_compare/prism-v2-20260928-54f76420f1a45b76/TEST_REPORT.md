# Test And Runtime Evidence

## Automated Regression

```json
{
  "status": "PASS",
  "date_utc": "2026-09-28",
  "command": "OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TMPDIR=/dev/shm /home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests/v2 -p 'test_*.py'",
  "implementation_hash": "491dd1a2ed1413274018972631011a7c707f86e665aceb4d31a5cf24a4192b59",
  "tested_source_commit": "7a39ed29c42defa9c09cf1fd3212c7ea4c3039fa",
  "experiment_source_commit": "410de2d9b5d3cf380d0d759ea7652bb541956fd6",
  "tests_run": 236,
  "elapsed_seconds": 113.988,
  "failures": 0,
  "errors": 0,
  "exit_code": 0,
  "warnings": [
    "Matplotlib/NumPy generic timedelta deprecation warning",
    "Streamlit missing ScriptRunContext in bare mode"
  ],
  "interpretation": "IMPLEMENTATION_FIXTURES_NOT_MAIN_HISTORICAL_PERFORMANCE"
}
```

Fixture outcomes are implementation checks, not profitable historical evidence.

## Actual Cell Runtime

| Strategy | Split | Cost | Seconds | Peak Process RSS Bytes | API Calls |
| --- | --- | --- | ---: | ---: | ---: |
| A0_V2_F0 | validation | base | 75.508 | 1842053120 | 0 |
| A0_V2_F0 | validation | stress | 73.807 | 1842823168 | 0 |
| A0_V2_F0 | test | base | 145.348 | 1843355648 | 0 |
| A0_V2_F0 | test | stress | 153.075 | 1843871744 | 0 |
| B0_PRISM_A_SHARE_V1 | validation | base | 76.954 | 1841635328 | 0 |
| B0_PRISM_A_SHARE_V1 | validation | stress | 75.983 | 1844928512 | 0 |
| B0_PRISM_A_SHARE_V1 | test | base | 156.918 | 1845452800 | 0 |
| B0_PRISM_A_SHARE_V1 | test | stress | 157.160 | 1845444608 | 0 |
| C0_V2_EXPOSURE_CONTROL | validation | base | 74.448 | 1845977088 | 0 |
| C0_V2_EXPOSURE_CONTROL | validation | stress | 74.480 | 1845444608 | 0 |
| C0_V2_EXPOSURE_CONTROL | test | base | 152.327 | 1845977088 | 0 |
| C0_V2_EXPOSURE_CONTROL | test | stress | 148.506 | 1845444608 | 0 |

## Integrity

All successful cells are hash-verified before reporting; actual source/configuration is frozen before test.
Failed partial attempts are preserved separately; completed matching cells are reused without opening a different strategy search.
