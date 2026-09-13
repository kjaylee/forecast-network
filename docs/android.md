# Android shell (Solana Seeker)

`apps/android` wraps the production site in a Capacitor 8 WebView and adds the
native pieces a Seeker owner expects. Package `xyz.eastsea.forecast`, minimum
Android 7.0 (API 24), portrait only.

## Native surface

| Capability | Where | Notes |
| --- | --- | --- |
| Mobile Wallet Adapter | `MobileWalletPlugin.kt` | Seed Vault sign-in (`signMessagesDetached`), Devnet memo attestation (`signTransactions` + on-device RPC submit). The wallet auth token is kept in app prefs so the connect sheet is skipped after a restart. MWA timeout 180 s because Seed Vault asks for a physical double tap. |
| Share sheet | `@capacitor/share` + `@capacitor/filesystem`, `public/native-share.mjs` | The WebView has no Web Share API; the bridge fills `navigator.share`/`canShare`, writes the profile-card PNG to the app cache and hands it to Android as a content uri. "Save PNG" routes to the same sheet inside the shell. |
| App Links | manifest intent-filter, `public/.well-known/assetlinks.json`, `public/native-links.mjs` | `https://forecast.eastsea.xyz/forecasts/*`, `/creators/*`, `/activity`, `/explore` open in the app once Android verifies the fingerprints. The page routes the launch url and later `appUrlOpen` events through its own `navigate()`; only same-origin urls are honoured. |
| Result notifications | `ResultNotifications.kt` | No push service. A WorkManager job (every 30 min, plus once when the app leaves the foreground) reads `GET /api/activity` with the WebView's own session cookie and raises a local notification per unread item it has not shown before. Tapping opens the forecast through the app link. Requires `POST_NOTIFICATIONS` on Android 13+. |
| System bars | `MainActivity.java` | Edge-to-edge insets, bars painted in page/tab-bar colours. |

## Wallet network

The Seeker wallet signs only for the network it is set to. For the Devnet
attestation the wallet must be on **Settings → Network → Devnet**; on Mainnet it
shows "network mismatch" and refuses the memo transaction.

## Build

```sh
cd apps/android
npm install && npx cap sync android
cd android
JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Use `install -r`; uninstalling wipes the WebView cookie jar and signs the user out.

### Release signing

The keystore is not in the repository. Export the environment before
`assembleRelease`; without it the release build is unsigned.

```sh
export FORECAST_KEYSTORE_FILE=/path/to/forecast-release.jks
export FORECAST_KEYSTORE_PASSWORD=...   # FORECAST_KEY_ALIAS defaults to "forecast"
./gradlew assembleRelease
```

The release certificate fingerprint is listed in
`apps/web/public/.well-known/assetlinks.json` next to the debug one, so both
builds verify app links. After changing either certificate, redeploy the web
app and run `adb shell pm verify-app-links --re-verify xyz.eastsea.forecast`.

## Verifying on a device

```sh
adb shell pm get-app-links xyz.eastsea.forecast            # expect: verified
adb shell am start -a android.intent.action.VIEW -d https://forecast.eastsea.xyz/forecasts/<id>
adb logcat -s ForecastWallet ForecastNotify                # wallet + notification diagnostics
```
