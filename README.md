# kronos-runner-public

Public runner workspace for the **KRONOS** Nifty50 bot: 16 GB GitHub Actions
workflows and the mobile APK builder.

This repository is public on purpose — it contains **only** workflow
definitions and build tooling. No strategy logic, keys or account credentials
belong here.

## Workflows

| Workflow | File | Purpose |
| --- | --- | --- |
| Build Android APK | `.github/workflows/build-apk.yml` | Builds and uploads the KRONOS mobile APK on a GitHub-hosted runner |

### Build Android APK

Triggers on manual dispatch or a push that touches `mobile/**`.

The workflow runs a preflight check for `mobile/pubspec.yaml`. Until the Flutter
app is added under `mobile/`, the job logs a warning and skips the build rather
than failing. Once the app is in place it sets up Java 17 + Flutter, runs
`flutter build apk --<release|profile|debug> --split-per-abi`, and uploads the
APKs as the `kronos-apk` artifact (14-day retention).

## Runner capacity

GitHub-hosted `ubuntu-latest` runners provide roughly 4 vCPU / 16 GB RAM. The
workflow prints `nproc`, `free -h` and `df -h` in its first step so the reported
capacity is always visible in the job log.

## Adding the mobile app

```
mobile/
├── pubspec.yaml
└── lib/…
```

Push to `main` and the APK build runs automatically.
