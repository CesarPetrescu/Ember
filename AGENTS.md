# Repository Instructions

## Android APK Versioning

- For every Android APK update, including watch, glasses, G2, or other wearable-targeted changes, bump `versionName` by `+0.0.1` and increment `versionCode` by `+1`.
- The Android app version is defined in `android/app/build.gradle.kts`.
- Build the APK after version changes with `./gradlew :app:assembleDebug` from `android/`.
- When installing to multiple ADB devices, always target each device explicitly with `adb -s <serial> install -r ...`.
