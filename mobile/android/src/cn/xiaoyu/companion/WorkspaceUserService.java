package cn.xiaoyu.companion;

import android.util.Log;

import org.json.JSONObject;

/**
 * Shell-uid side of the phone workspace, started by Shizuku.
 *
 * Shizuku launches this class in its own process with the shell identity and
 * the app's APK on the classpath. Everything that needs that identity - the
 * trusted virtual display, launching apps into it, directed input - happens
 * here; the app process only sends one JSON string at a time.
 */
public final class WorkspaceUserService extends IWorkspaceService.Stub {
    private static final String TAG = "XiaoyuWorkspace";

    private final Object lock = new Object();
    private WorkspaceDisplay display;

    public WorkspaceUserService() {
        super();
        Log.i(TAG, "workspace user service created");
    }

    @Override
    public String request(String json) {
        JSONObject request;
        try {
            request = new JSONObject(json == null || json.isEmpty() ? "{}" : json);
        } catch (Exception error) {
            return failed("workspace_bad_request", "工作区请求不是有效 JSON。");
        }
        String op = request.optString("op", "");
        try {
            if ("start".equals(op)) return start(request);
            if ("close".equals(op)) return close(request);
            synchronized (lock) {
                if (display == null) {
                    return failed("workspace_not_started", "副屏服务还没有建立工作区，先发送 start。");
                }
                return display.handle(request).toString();
            }
        } catch (Throwable error) {
            Log.w(TAG, "workspace request failed: " + op, error);
            return failed("workspace_error", op + " 失败：" + error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    private String start(JSONObject request) {
        synchronized (lock) {
            try {
                if (display != null) {
                    JSONObject answer = display.describe();
                    answer.put("status", "completed").put("event", "ready").put("reused", true);
                    return answer.toString();
                }
                int width = request.optInt("width", 0);
                int height = request.optInt("height", 0);
                int dpi = request.optInt("dpi", 0);
                if (width <= 0 || height <= 0 || dpi <= 0) {
                    return failed("workspace_bad_geometry", "start 需要 width/height/dpi。");
                }
                display = WorkspaceDisplay.start(width, height, dpi);
                JSONObject answer = display.describe();
                answer.put("status", "completed").put("event", "ready").put("reused", false);
                Log.i(TAG, "workspace display " + display.displayId() + " " + width + "x" + height + "/" + dpi);
                return answer.toString();
            } catch (Throwable error) {
                display = null;
                Log.w(TAG, "cannot create workspace display", error);
                return failed("workspace_create_failed", "无法创建副屏：" + error.getClass().getSimpleName() + ": " + error.getMessage());
            }
        }
    }

    private String close(JSONObject request) {
        synchronized (lock) {
            if (display == null) {
                return failed("workspace_not_started", "当前没有工作区。");
            }
            int id = display.displayId();
            display.close();
            display = null;
            Log.i(TAG, "workspace display " + id + " released");
            try {
                return new JSONObject().put("status", "completed").put("display_id", id).put("closed", true)
                        .put("id", request.optString("id", "")).toString();
            } catch (Exception error) {
                return "{\"status\":\"completed\",\"closed\":true}";
            }
        }
    }

    private static String failed(String code, String output) {
        try {
            return new JSONObject().put("status", "failed").put("code", code).put("output", output).toString();
        } catch (Exception error) {
            return "{\"status\":\"failed\",\"code\":\"workspace_error\"}";
        }
    }
}
