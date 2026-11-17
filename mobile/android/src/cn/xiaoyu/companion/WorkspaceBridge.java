package cn.xiaoyu.companion;

import android.content.ComponentName;
import android.content.Context;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.os.IBinder;
import android.os.RemoteException;
import android.util.DisplayMetrics;
import android.util.Log;

import org.json.JSONObject;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import rikka.shizuku.Shizuku;

/**
 * App-side handle to the shell-uid workspace process.
 *
 * The display can only be created by the shell identity and only lives inside
 * the process that created it, so this class never creates one. It asks Shizuku
 * for a UserService, sends one JSON request at a time over the binder, and
 * reports plainly when Shizuku is missing, unauthorized, or not bound.
 */
final class WorkspaceBridge {
    private static final String TAG = "XiaoyuWorkspace";
    static final int PERMISSION_REQUEST = 0x5756; // "WV"

    private static final Object LOCK = new Object();
    private static final ArrayBlockingQueue<Boolean> READY = new ArrayBlockingQueue<>(8);
    private static final AtomicBoolean REQUESTED = new AtomicBoolean(false);
    private static final AtomicBoolean LISTENING = new AtomicBoolean(false);

    private static volatile IWorkspaceService service;
    private static volatile boolean binding;
    private static volatile String lastError = "";
    private static Shizuku.UserServiceArgs args;
    private static Context appContext;
    private static int width;
    private static int height;
    private static int dpi;

    private static final ServiceConnection CONNECTION = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder binder) {
            IWorkspaceService bound = IWorkspaceService.Stub.asInterface(binder);
            if (bound == null) {
                lastError = "Shizuku 返回的副屏服务无法识别。";
                binding = false;
                READY.offer(false);
                return;
            }
            service = bound;
            binding = false;
            lastError = "";
            Log.i(TAG, "workspace service connected");
            READY.offer(true);
            PhoneService.refreshCapabilities();
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            service = null;
            PhoneWorkspaceManager.invalidate();
            binding = false;
            lastError = "副屏服务已断开（Shizuku 可能重启了）。";
            Log.w(TAG, "workspace service disconnected");
            PhoneService.refreshCapabilities();
        }
    };

    private WorkspaceBridge() {}

    /** Called from the app process before anything else; safe to call repeatedly. */
    static void prepare(Context context) {
        if (context == null) return;
        appContext = context.getApplicationContext();
        if (args == null) {
            try {
                args = new Shizuku.UserServiceArgs(
                        new ComponentName(appContext.getPackageName(), WorkspaceUserService.class.getName()))
                        .daemon(false)
                        .debuggable(false)
                        .version(1)
                        .processNameSuffix("workspace");
            } catch (Throwable error) {
                lastError = "Shizuku 客户端初始化失败：" + error.getClass().getSimpleName();
                Log.w(TAG, "cannot build UserServiceArgs", error);
            }
        }
        listenForPermission();
    }

    private static void listenForPermission() {
        if (!LISTENING.compareAndSet(false, true)) return;
        try {
            Shizuku.addRequestPermissionResultListener((requestCode, grantResult) -> {
                if (requestCode != PERMISSION_REQUEST) return;
                if (grantResult == PackageManager.PERMISSION_GRANTED) {
                    lastError = "";
                    Log.i(TAG, "shizuku permission granted");
                    connect(appContext, false);
                } else {
                    lastError = "Shizuku 授权被拒绝：手机工作区不可用。";
                    Log.w(TAG, "shizuku permission denied");
                }
                PhoneService.refreshCapabilities();
            });
        } catch (Throwable error) {
            LISTENING.set(false);
            Log.w(TAG, "cannot listen for shizuku permission", error);
        }
    }

    static String lastError() {
        return lastError;
    }

    static boolean available() {
        return service != null;
    }

    /** True when Shizuku itself is installed and running on this phone. */
    static boolean shizukuAlive() {
        try {
            return Shizuku.pingBinder();
        } catch (Throwable error) {
            return false;
        }
    }

    static boolean permitted() {
        try {
            return Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable error) {
            return false;
        }
    }

    /**
     * Binds the workspace process, asking Shizuku for authorization first when
     * it has not been granted. The permission dialog needs a foreground
     * activity, so call this from an Activity when possible.
     */
    static boolean connect(Context context, boolean allowPermissionRequest) {
        prepare(context);
        if (args == null) return false;
        if (service != null) return true;
        if (!shizukuAlive()) {
            lastError = "Shizuku 服务没有运行：需要在手机上的 Shizuku 里重新启动服务。";
            Log.i(TAG, "shizuku binder not alive");
            return false;
        }
        if (!permitted()) {
            lastError = "小鱼还没有被 Shizuku 授权。";
            if (allowPermissionRequest && !REQUESTED.getAndSet(true)) {
                try {
                    Log.i(TAG, "requesting shizuku permission");
                    Shizuku.requestPermission(PERMISSION_REQUEST);
                    lastError = "已请求 Shizuku 授权，请在手机上点“允许”。";
                } catch (Throwable error) {
                    lastError = "请求 Shizuku 授权失败：" + error.getClass().getSimpleName();
                }
            }
            return false;
        }
        synchronized (LOCK) {
            if (service != null) return true;
            if (binding) return false;
            binding = true;
        }
        try {
            Log.i(TAG, "binding workspace user service");
            Shizuku.bindUserService(args, CONNECTION);
        } catch (Throwable error) {
            binding = false;
            lastError = "绑定副屏服务失败：" + error.getClass().getSimpleName() + ": " + error.getMessage();
            Log.w(TAG, "bindUserService failed", error);
            return false;
        }
        return true;
    }

    /** Waits briefly for a bind started by {@link #connect}. */
    static boolean awaitBound(long timeoutMs) {
        if (service != null) return true;
        try {
            Boolean ok = READY.poll(timeoutMs, TimeUnit.MILLISECONDS);
            return Boolean.TRUE.equals(ok) && service != null;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    static void disconnect() {
        PhoneWorkspaceManager.invalidate();
        if (args == null) return;
        synchronized (LOCK) {
            binding = false;
        }
        service = null;
        try {
            Shizuku.unbindUserService(args, CONNECTION, true);
        } catch (Throwable error) {
            Log.w(TAG, "unbindUserService failed", error);
        }
        READY.offer(false);
    }

    /**
     * Sends one request to the workspace process and parses the answer.
     * Callers should not run this on the main thread.
     */
    static JSONObject call(JSONObject request, long timeoutMs) {
        JSONObject answer = new JSONObject();
        IWorkspaceService target = service;
        try {
            if (target == null) {
                return answer.put("status", "failed").put("code", "workspace_unavailable")
                        .put("output", lastError.isEmpty()
                                ? "手机工作区还没有建立：需要 Shizuku 授权并连接副屏服务。"
                                : lastError);
            }
            if (appContext != null && ("start".equals(request.optString("op")))) {
                DisplayMetrics metrics = appContext.getResources().getDisplayMetrics();
                width = Math.max(width, metrics.widthPixels);
                height = Math.max(height, metrics.heightPixels);
                dpi = dpi == 0 ? metrics.densityDpi : dpi;
                request.put("width", width).put("height", height).put("dpi", dpi);
            }
            long started = System.currentTimeMillis();
            String raw = target.request(request.toString());
            long elapsed = System.currentTimeMillis() - started;
            if (raw == null || raw.trim().isEmpty()) {
                return answer.put("status", "failed").put("code", "workspace_no_answer")
                        .put("output", "副屏服务没有返回结果。");
            }
            answer = new JSONObject(raw);
            answer.put("elapsed_ms", elapsed);
            return answer;
        } catch (RemoteException error) {
            lastError = "副屏服务连接中断，已丢弃这次操作。";
            return failure(answer, "workspace_disconnected", lastError);
        } catch (Throwable error) {
            return failure(answer, "workspace_error", error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    private static JSONObject failure(JSONObject answer, String code, String output) {
        try {
            return answer.put("status", "failed").put("code", code).put("output", output);
        } catch (Exception ignored) {
            return answer;
        }
    }
}
