package cn.xiaoyu.companion;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Routes workspace actions to the Shizuku-backed shell process and keeps the
 * small amount of bookkeeping the cloud needs (workspace id, display id,
 * geometry). It never creates a display itself: without Shizuku the answer is
 * an explicit "unavailable", never a silent fallback to the main screen.
 */
final class PhoneWorkspaceManager {
    private static final Object LOCK = new Object();
    private static volatile JSONObject current;
    private static volatile String lastError = "";

    private PhoneWorkspaceManager() {}

    static void invalidate() { current = null; }

    static void prepare(Context context) {
        WorkspaceBridge.prepare(context);
    }

    /** Called from the app UI (Activity context) so Shizuku can show its dialog. */
    static boolean connect(Context context, boolean allowPermissionRequest) {
        boolean started = WorkspaceBridge.connect(context, allowPermissionRequest);
        if (started) WorkspaceBridge.awaitBound(3000L);
        lastError = WorkspaceBridge.lastError();
        return WorkspaceBridge.available();
    }

    static boolean available() {
        return WorkspaceBridge.available();
    }

    static String state() {
        if (WorkspaceBridge.available()) return "connected";
        if (!WorkspaceBridge.shizukuAlive()) return "shizuku_offline";
        if (!WorkspaceBridge.permitted()) return "unauthorized";
        return "disconnected";
    }

    static String lastError() {
        return lastError == null || lastError.isEmpty() ? WorkspaceBridge.lastError() : lastError;
    }

    static JSONArray capabilities() {
        JSONArray rows = new JSONArray();
        JSONObject workspace = current;
        if (workspace == null || !WorkspaceBridge.available()) return rows;
        try {
            rows.put(new JSONObject(workspace.toString()));
        } catch (Exception ignored) {
        }
        return rows;
    }

    static boolean handles(String op) {
        return op != null && (op.startsWith("workspace_") || op.equals("screenshot") || op.equals("tap")
                || op.equals("swipe") || op.equals("text") || op.equals("back") || op.equals("home")
                || op.equals("recents") || op.equals("open_app") || op.equals("info"));
    }

    /** Runs on a background thread: binder calls block. */
    static JSONObject handle(Context context, String op, JSONObject args) {
        JSONObject answer = new JSONObject();
        try {
            if (!WorkspaceBridge.available()) {
                String detail = lastError();
                return answer.put("status", "failed").put("code", "workspace_unavailable")
                        .put("target", "workspace")
                        .put("output", detail.isEmpty()
                                ? "手机工作区不可用：需要 Shizuku 服务在运行、并已授权小鱼。"
                                : "手机工作区不可用：" + detail);
            }
            if (!"workspace_open".equals(op)) {
                JSONObject live = current;
                if (live == null || !live.optString("workspace_id").equals(args.optString("workspace_id"))
                        || (!op.startsWith("workspace_")
                            && !live.optString("session_id").equals(args.optString("session_id")))) {
                    return answer.put("status", "failed").put("code", "workspace_changed")
                        .put("target", "workspace").put("output", "工作区已变化，请读取最新工作区后再操作。");
                }
            }
            String action;
            JSONObject request = new JSONObject();
            switch (op) {
                case "workspace_open":
                    action = "open";
                    request.put("package", args.optString("package", ""));
                    request.put("separate_task", true);
                    break;
                case "workspace_status":
                    action = "status";
                    break;
                case "workspace_close":
                    return close(answer);
                case "workspace_pause":
                case "workspace_resume":
                    return answer.put("status", "failed").put("code", "workspace_unsupported")
                            .put("target", "workspace")
                            .put("output", "暂停/恢复还没有实现：副屏会一直保持到关闭为止。");
                case "open_app":
                    action = "open";
                    request.put("package", args.optString("package", ""));
                    request.put("separate_task", true);
                    break;
                case "back":
                    action = "key";
                    request.put("keyevent", "4");
                    break;
                case "home":
                case "recents":
                    return answer.put("status", "failed").put("code", "workspace_unsupported")
                            .put("target", "workspace")
                            .put("output", "home/recents 是全局按键，可能影响主人主屏，工作区暂不支持；"
                                    + "用 back 或直接 open 别的应用。");
                default:
                    action = op;
                    for (String key : new String[]{"x", "y", "x1", "y1", "x2", "y2", "duration_ms", "text"}) {
                        if (args.has(key)) request.put(key, args.get(key));
                    }
            }
            if (!"close".equals(action)) {
                JSONObject started = ensureStarted();
                if (started != null) return started;
            }
            request.put("op", action);
            long timeout = "open".equals(action) ? 25000L : ("screenshot".equals(action) ? 15000L : 10000L);
            JSONObject result = WorkspaceBridge.call(request, timeout);
            return normalize(answer, result);
        } catch (Exception error) {
            try {
                return answer.put("status", "failed").put("code", "workspace_error").put("target", "workspace")
                        .put("output", error.getClass().getSimpleName() + ": " + error.getMessage());
            } catch (Exception ignored) {
                return answer;
            }
        }
    }

    private static JSONObject ensureStarted() {
        JSONObject workspace = current;
        if (workspace != null) return null;
        try {
            JSONObject start = new JSONObject().put("op", "start");
            JSONObject result = WorkspaceBridge.call(start, 20000L);
            if (!"completed".equals(result.optString("status"))) {
                return new JSONObject().put("status", "failed")
                        .put("code", result.optString("code", "workspace_create_failed"))
                        .put("target", "workspace")
                        .put("output", result.optString("output", "无法创建副屏。"));
            }
            int displayId = result.optInt("display_id", 0);
            if (displayId <= 0) {
                return new JSONObject().put("status", "failed").put("code", "workspace_create_failed")
                        .put("target", "workspace").put("output", "副屏创建没有返回 display_id。");
            }
            JSONObject row = new JSONObject()
                    .put("workspace_id", "ws-" + displayId)
                    .put("session_id", java.util.UUID.randomUUID().toString())
                    .put("display_id", displayId)
                    .put("width", result.optInt("width"))
                    .put("height", result.optInt("height"))
                    .put("dpi", result.optInt("dpi"))
                    .put("state", "running")
                    .put("created_at", System.currentTimeMillis());
            current = row;
            PhoneService.refreshCapabilities();
            return null;
        } catch (Exception error) {
            try {
                return new JSONObject().put("status", "failed").put("code", "workspace_create_failed")
                        .put("target", "workspace")
                        .put("output", "创建副屏失败：" + error.getClass().getSimpleName() + ": " + error.getMessage());
            } catch (Exception ignored) {
                return new JSONObject();
            }
        }
    }

    private static JSONObject close(JSONObject answer) throws Exception {
        JSONObject result = WorkspaceBridge.call(new JSONObject().put("op", "close"), 10000L);
        current = null;
        PhoneService.refreshCapabilities();
        return normalize(answer, result);
    }

    private static JSONObject normalize(JSONObject answer, JSONObject result) throws Exception {
        answer = new JSONObject(result.toString());
        answer.put("target", "workspace");
        JSONObject workspace = current;
        if (workspace != null) {
            answer.put("workspace_id", workspace.optString("workspace_id"));
            answer.put("session_id", workspace.optString("session_id"));
            if (!answer.has("display_id")) answer.put("display_id", workspace.optInt("display_id"));
        }
        if (!"completed".equals(answer.optString("status")) && !answer.has("output")) {
            answer.put("output", "手机工作区操作没有完成。");
        }
        return answer;
    }
}
