# Working in this repository

This is an independent HRV early-warning research repository. The parent directory is only its current workspace location; do not modify or depend on the surrounding VEC project.

- This project belongs to the user's independently formed team with friends. Do not infer NTU or any university/laboratory affiliation from other repositories, prior research, or the workspace path.
- Read `docs/protocol.md` and `configs/protocol.toml` before changing cohort, endpoint, or evaluation behavior. Record protocol revisions before model evaluation.
- Keep source datasets on their existing mounts. Generated patient-level files, waveform extracts and local configuration belong in ignored directories, never tracked source.
- Do not copy the legacy project's mount scripts or credentials. Preserve attribution for research ideas and confirm licensing before copying source code.
- Separate observed treatment initiation from clinical need or a septic-shock diagnosis. Report unknown data coverage and censoring explicitly.
- Split patients before fitting preprocessing or selecting models. Keep test results out of model and feature selection.
- Run relevant tests with `PYTHONPATH=src python -m unittest discover -s tests -v`.
- The current CLI supports `audit`, `protocol`, and `feasibility`. The latter checks first-use events and ECG header/file coverage. Do not describe training, ECG quality assessment, or the demonstration UI as complete until implemented and verified.
