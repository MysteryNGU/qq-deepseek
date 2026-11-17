package cn.xiaoyu.companion;

import android.content.AttributionSource;
import android.content.ComponentName;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.Intent;
import android.graphics.Bitmap;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.view.Surface;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.nio.ByteBuffer;

/**
 * One off-screen display owned by a shell-uid process.
 *
 * It is created inside a Shizuku UserService (or, for debugging, a plain
 * {@code app_process} main), because only the shell identity may create a
 * trusted display and because the display dies with the process that created
 * it. Frames are rendered into an ImageReader owned here, so a screenshot never
 * depends on the main screen or on {@code screencap}.
 *
 * Coordinates in requests and in images are this display's own pixels.
 */
final class WorkspaceDisplay {
    private static final int FLAG_PUBLIC = 1;
    private static final int FLAG_PRESENTATION = 2;
    private static final int FLAG_OWN_CONTENT_ONLY = 8;
    private static final int FLAG_SUPPORTS_TOUCH = 1 << 6;
    private static final int FLAG_ROTATES_WITH_CONTENT = 1 << 7;
    private static final int FLAG_DESTROY_CONTENT_ON_REMOVAL = 1 << 8;
    private static final int FLAG_TRUSTED = 1 << 10;
    private static final int FLAG_OWN_DISPLAY_GROUP = 1 << 11;
    private static final int FLAG_ALWAYS_UNLOCKED = 1 << 12;
    private static final int FLAG_TOUCH_FEEDBACK_DISABLED = 1 << 13;
    private static final int FLAG_OWN_FOCUS = 1 << 14;
    private static final int FLAG_DEVICE_DISPLAY_GROUP = 1 << 15;

    private final int width;
    private final int height;
    private final int dpi;
    private final Context context;
    private final ImageReader reader;
    private final HandlerThread frameThread;
    private VirtualDisplay display;
    private int displayId;

    private volatile Bitmap latest;
    private volatile long latestAt;
    private volatile long frameCount;
    private long lastCopy;

    private static final class ShellContext extends ContextWrapper {
        private static final String PACKAGE = "com.android.shell";

        ShellContext(Context base) {
            super(base);
        }

        @Override
        public String getPackageName() {
            return PACKAGE;
        }

        @Override
        public String getOpPackageName() {
            return PACKAGE;
        }

        @Override
        public Context getApplicationContext() {
            return this;
        }

        @Override
        public Context createPackageContext(String name, int flags) {
            return this;
        }

        @Override
        public AttributionSource getAttributionSource() {
            return new AttributionSource.Builder(android.os.Process.SHELL_UID).setPackageName(PACKAGE).build();
        }
    }

    private WorkspaceDisplay(int width, int height, int dpi, Context context, ImageReader reader, HandlerThread frameThread) {
        this.width = width;
        this.height = height;
        this.dpi = dpi;
        this.context = context;
        this.reader = reader;
        this.frameThread = frameThread;
    }

    static WorkspaceDisplay start(int width, int height, int dpi) throws Exception {
        if (width <= 0 || height <= 0 || dpi <= 0) throw new IllegalArgumentException("geometry");
        // ActivityThread.systemMain() needs a prepared main looper; the Shizuku
        // service process may or may not have prepared one already.
        if (Looper.myLooper() == null) {
            try {
                Looper.prepareMainLooper();
            } catch (IllegalStateException ignored) {
                // another thread already owns the main looper
            }
        }
        Context context = systemContext();
        HandlerThread frameThread = new HandlerThread("workspace-frames");
        frameThread.start();
        ImageReader reader = ImageReader.newInstance(width, height, 1, 3);
        WorkspaceDisplay workspace = new WorkspaceDisplay(width, height, dpi, context, reader, frameThread);
        reader.setOnImageAvailableListener(workspace::onFrame, new Handler(frameThread.getLooper()));
        try {
            workspace.open();
        } catch (Exception error) {
            workspace.close();
            throw error;
        }
        return workspace;
    }

    private void open() throws Exception {
        Constructor<DisplayManager> constructor = DisplayManager.class.getDeclaredConstructor(Context.class);
        constructor.setAccessible(true);
        DisplayManager manager = constructor.newInstance(context);
        int flags = FLAG_PUBLIC | FLAG_PRESENTATION | FLAG_OWN_CONTENT_ONLY
                | FLAG_SUPPORTS_TOUCH | FLAG_ROTATES_WITH_CONTENT | FLAG_DESTROY_CONTENT_ON_REMOVAL;
        if (Build.VERSION.SDK_INT >= 33) {
            flags |= FLAG_TRUSTED | FLAG_OWN_DISPLAY_GROUP | FLAG_ALWAYS_UNLOCKED | FLAG_TOUCH_FEEDBACK_DISABLED;
            if (Build.VERSION.SDK_INT >= 34) flags |= FLAG_OWN_FOCUS | FLAG_DEVICE_DISPLAY_GROUP;
        }
        Surface surface = reader.getSurface();
        display = manager.createVirtualDisplay("xiaoyu-workspace", width, height, dpi, surface, flags);
        displayId = display.getDisplay().getDisplayId();
        if (displayId <= 0) throw new IllegalStateException("display not created");
    }

    int displayId() {
        return displayId;
    }

    int width() {
        return width;
    }

    int height() {
        return height;
    }

    int dpi() {
        DisplayMetrics metrics = new DisplayMetrics();
        display.getDisplay().getMetrics(metrics);
        return metrics.densityDpi;
    }

    JSONObject describe() throws Exception {
        return new JSONObject().put("display_id", displayId).put("width", width).put("height", height)
                .put("dpi", dpi()).put("frames", frameCount).put("has_frame", latest != null);
    }

    private void onFrame(ImageReader source) {
        Image image = source.acquireLatestImage();
        if (image == null) return;
        try {
            frameCount++;
            long now = System.currentTimeMillis();
            // A full-frame copy is expensive, so keep at most four per second. The
            // newest copy is still what a screenshot returns when nothing changed.
            if (now - lastCopy < 250L && latest != null) return;
            lastCopy = now;
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int pixelStride = plane.getPixelStride();
            int rowStride = plane.getRowStride();
            int rowWidth = rowStride / pixelStride;
            Bitmap raw = Bitmap.createBitmap(rowWidth, image.getHeight(), Bitmap.Config.ARGB_8888);
            raw.copyPixelsFromBuffer(buffer);
            latest = rowWidth == width ? raw : Bitmap.createBitmap(raw, 0, 0, width, image.getHeight());
            latestAt = now;
        } catch (Throwable ignored) {
            // A single bad frame must not kill the workspace.
        } finally {
            image.close();
        }
    }

    /** Executes one workspace operation and returns the answer to send back. */
    JSONObject handle(JSONObject request) throws Exception {
        String op = request.optString("op", "");
        JSONObject answer = new JSONObject().put("display_id", displayId);
        if (!request.optString("id", "").isEmpty()) answer.put("id", request.optString("id"));
        switch (op) {
            case "status":
                answer.put("status", "completed").put("frames", frameCount).put("has_frame", latest != null);
                break;
            case "open": {
                String packageName = request.optString("package", "");
                if (packageName.isEmpty()) {
                    answer.put("status", "failed").put("code", "workspace_bad_request")
                            .put("output", "缺少 package。");
                    break;
                }
                Intent launch = context.getPackageManager().getLaunchIntentForPackage(packageName);
                if (launch == null || launch.getComponent() == null) {
                    answer.put("status", "failed").put("code", "workspace_no_launch_activity")
                            .put("output", "这台手机没有可启动的 " + packageName + "。");
                    break;
                }
                ComponentName component = launch.getComponent();
                boolean separate = request.optBoolean("separate_task", true);
                String[] command = separate
                        ? new String[]{"am", "start", "--display", String.valueOf(displayId), "--activity-multiple-task",
                            "-n", component.flattenToShortString()}
                        : new String[]{"am", "start", "--display", String.valueOf(displayId),
                            "-n", component.flattenToShortString()};
                int code = exec(command);
                answer.put("status", code == 0 ? "completed" : "failed").put("package", packageName)
                        .put("activity", component.getClassName()).put("separate_task", separate);
                if (code != 0) answer.put("code", "workspace_launch_failed");
                break;
            }
            case "screenshot": {
                answer = screenshot(answer);
                break;
            }
            case "tap": {
                answer.put("status", exec("input", "-d", String.valueOf(displayId), "tap",
                        String.valueOf(request.getInt("x")), String.valueOf(request.getInt("y"))) == 0
                        ? "completed" : "failed");
                break;
            }
            case "swipe": {
                answer.put("status", exec("input", "-d", String.valueOf(displayId), "swipe",
                        String.valueOf(request.getInt("x1")), String.valueOf(request.getInt("y1")),
                        String.valueOf(request.getInt("x2")), String.valueOf(request.getInt("y2")),
                        String.valueOf(request.optInt("duration_ms", 300))) == 0 ? "completed" : "failed");
                break;
            }
            case "text": {
                String text = request.optString("text", "");
                if (text.isEmpty()) {
                    answer.put("status", "failed").put("code", "workspace_bad_request").put("output", "缺少 text。");
                    break;
                }
                if (!isAscii(text)) {
                    answer.put("status", "failed").put("code", "workspace_text_encoding")
                            .put("output", "副屏输入目前只支持 ASCII；中文输入还没接入。");
                    break;
                }
                answer.put("status", exec("input", "-d", String.valueOf(displayId), "text", text) == 0
                        ? "completed" : "failed");
                break;
            }
            case "key": {
                answer.put("status", exec("input", "-d", String.valueOf(displayId), "keyevent",
                        String.valueOf(request.optString("keyevent", "4"))) == 0 ? "completed" : "failed");
                break;
            }
            default:
                answer.put("status", "failed").put("code", "workspace_unknown_op")
                        .put("output", "未知工作区操作：" + op);
        }
        return answer;
    }

    private JSONObject screenshot(JSONObject answer) throws Exception {
        long deadline = System.currentTimeMillis() + 1200L;
        while (latest == null && System.currentTimeMillis() < deadline) Thread.sleep(40);
        Bitmap bitmap = latest;
        if (bitmap == null) {
            return answer.put("status", "failed").put("code", "workspace_no_frame")
                    .put("output", "副屏还没有产生任何画面。");
        }
        long started = System.currentTimeMillis();
        long freshUntil = started + 800L;
        while (System.currentTimeMillis() < freshUntil && latestAt < started) Thread.sleep(30);
        bitmap = latest;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.PNG, 100, bytes);
        byte[] png = bytes.toByteArray();
        if (png.length > 900 * 1024) {
            return answer.put("status", "failed").put("code", "workspace_frame_too_large")
                    .put("output", "副屏截图超过 900 KiB，无法通过一次调用传回。");
        }
        return answer.put("status", "completed").put("width", bitmap.getWidth())
                .put("height", bitmap.getHeight())
                .put("frame_age_ms", Math.max(0L, System.currentTimeMillis() - latestAt))
                .put("frame_count", frameCount)
                .put("image_png", Base64.encodeToString(png, Base64.NO_WRAP));
    }

    private static boolean isAscii(String text) {
        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) > 0x7e || text.charAt(i) < 0x20) return false;
        }
        return true;
    }

    private static int exec(String... command) {
        try {
            Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
            StringBuilder text = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    if (text.length() < 2000) text.append(line).append('\n');
                }
            }
            int code = process.waitFor();
            android.util.Log.i("XiaoyuWorkspace", String.join(" ", command) + " -> " + code + " " + text.toString().trim());
            return code;
        } catch (Exception error) {
            android.util.Log.w("XiaoyuWorkspace", "exec failed: " + String.join(" ", command), error);
            return -1;
        }
    }

    private static Context systemContext() throws Exception {
        Class<?> activityThread = Class.forName("android.app.ActivityThread");
        Method systemMain = activityThread.getDeclaredMethod("systemMain");
        systemMain.setAccessible(true);
        Object thread = systemMain.invoke(null);
        Method getSystemContext = activityThread.getDeclaredMethod("getSystemContext");
        getSystemContext.setAccessible(true);
        return new ShellContext((Context) getSystemContext.invoke(thread));
    }

    void close() {
        try {
            if (display != null) display.release();
        } catch (Exception ignored) {
        }
        display = null;
        try {
            reader.close();
        } catch (Exception ignored) {
        }
        try {
            frameThread.quitSafely();
        } catch (Exception ignored) {
        }
        Bitmap bitmap = latest;
        latest = null;
        if (bitmap != null) bitmap.recycle();
    }
}
