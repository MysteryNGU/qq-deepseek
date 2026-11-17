# Android native shell contract

Package `cn.xiaoyu.companion`, minSdk26, target35. No third-party Java dependencies. Compile against Android35 android.jar; assets/index.html is the local web UI.

- `Native.config()` returns a JSON string including `{server,paired,speech,device_id,device_name,version,version_code}`. Token is stored in private SharedPreferences and excluded from config. Pairing stores the returned device ID; existing installations recover it from `/mobile/state.mobile_device.id` without re-pairing.
- `Native.request(id,method,path,bodyJSON)` asynchronously invokes fixed HTTPS origin. Callback `window.onNativeResult(id,status,JSONstring)`. Successful `/mobile/pair` stores token. `/mobile/image/{handle}` returns JSON `{mime,base64}` instead of binary.
- `Native.pickImage()` opens the system Photo Picker with up to8 images; older devices fall back to a gallery picker. Each image invokes `window.onPickedImage({name,mime,base64})`, or `{error}`. `Native.takePhoto()` invokes the system camera with a temporary, individually granted content URI. Inputs are capped32MiB/64million pixels, EXIF rotation is applied, and output is resized to1600px longest edge, capped8MiB, PNG for alpha/JPEG otherwise. No whole-gallery or camera permission is requested by the app.
- `Native.startConnection()` starts foreground message service; paired app also starts it on resume.
- `Native.openSettings()` opens Android battery settings; manufacturers may impose further background restrictions.
- `Native.openPhoneSettings()` opens Android Accessibility settings. `Native.config()` additionally returns `phone_enabled`, reflecting a connected Accessibility service, not a stored toggle. The user enables **小鱼 · 手机操作** in Android; the app does not enable itself or use root.
- `Native.setSpeech(bool)` saves the automatic-reading preference (defaulttrue, existingfalse preserved). `Native.speak(text)` queues cloud speech from `/mobile/speech` independently of this preference so manual playback still works; `Native.stopSpeech()` immediately stops playback and discards queued/stale audio. HTML decides when to enqueue automatic reading. Cloud failure emits a visible `onSpeechState({state:'error',message,fallback})` and tries installed Android Chinese TTS. States also include `loading`, `playing`, `idle`. No voice-engine credentials are included in the app. Voice input/recording is not implemented or requested.
- Foreground service delivers direct SSE JSON via `window.onServerEvent(object)`. Connection changes are `{type:'connection',connected:boolean}`. Front-facing Activity gets events without duplicate OS notifications; background assistant messages create notifications. Clicking one opens app.

First connection seeds known history silently. Reconnect fetches state and notifies unseen assistant IDs. Seen IDs are persisted (last500); catchup is bounded by the server's history response. It does not run the model. Android force-stop, network loss and vendor battery policy can interrupt receiving; reopening reconnects and catches up. No claim of guaranteed continuous background delivery.

History/events retain `source` and `delivery` across devices. System notifications require `delivery.targets` to contain `all` or `mobile:<this device_id>`; old messages without delivery retain their previous behavior. Non-target messages are still forwarded to the UI and remembered, so reconnecting does not alert on them later. Replayed IDs carry `previously_received:true` for the UI to suppress repeated voice playback while refreshing shared history. HTML also checks the delivery target before automatic voice playback; manual playback remains explicit.

Foreground service uses remoteMessaging permission/type, as documented at https://developer.android.com/develop/background-work/services/fgs/service-types. BOOT_COMPLETED/MY_PACKAGE_REPLACED attempts resume only when paired and catches OS denial. Foreground service notification is always declared; Android13+ notification permission is requested in app.

TLS uses Android's normal certificate verification with no trust bypass. No token/password is compiled into the APK. External links open the system browser; bridge-bearing WebView loads only bundled assets.

## Phone actions

`PhoneService` registers `/mobile/device` capabilities when Accessibility connects/disconnects, the activity resumes, and the SSE connection becomes ready. It receives targeted `phone_control` events through the existing foreground message service, so the chat screen need not be open. Supported operations are `info`, `screenshot`, `tap`, `swipe`, `text`, `back`, `home`, `recents`, and `open_app`. Screenshot requires Android11/API30; older supported devices omit it from capabilities.

`info` returns the active package, physical screen dimensions and at most180 accessibility nodes, with text bounded to240 characters. Password nodes omit text and description. Coordinates and screenshot PNGs are unscaled physical display pixels. Screenshots over8MiB or16million pixels report failure rather than silently changing coordinates. Android protected windows may reject capture. `text` replaces the focused editable field; `open_app` requires the launchable package name.

Gesture receipts come from Android completion/cancellation callbacks. Text/global-action receipts distinguish Android acceptance from observed resulting UI; use `info`/`screenshot` to inspect. Results post to `/mobile/control-result`. IDs are persisted before actions and duplicate IDs never replay; only one action executes at a time. `phone_cancel` ends the pending receipt, but cannot undo an already applied action. Timeout/disconnection reports failure. Failed receipt network delivery is not retried as a physical action.

## Local build

Run `.venv/Scripts/python.exe tools/build-companion-android.py` from the repository. The script uses SDK35 in `data/android-toolchain`, the installed JDK8 compiler, and a complete JRE17 under `data/android-toolchain/jre17` for D8 and apksigner. A complete JDK under `data/android-toolchain/jdk` takes precedence. The Cubism editor's reduced runtime lacks modules needed by D8.

The installed JRE is Azul Zulu17.70+113 (17.0.21.0.3), downloaded from its official CDN. Its official package metadata is saved in `data/android-toolchain/jre17-official-metadata.json`; the ZIP SHA-256 was verified against the official API: `c5a32fe9b1d2ff07ac4991b57a72d707ad97d54a9cc1bd2f8f005bfd1498d460`. Metadata URL: https://api.azul.com/metadata/v1/zulu/packages/c4fae530-35c9-44ce-95c3-6f0744cf0be7

Output: `data/android-build/output/Xiaoyu-0.2.0.apk`. The persistent local signing key is kept in the user's `.android/xiaoyu-signing` directory, outside the source and APK. Build completion includes apksigner verification and zip alignment validation. Keep this key for installable updates.
