package cn.xiaoyu.companion;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;

/**
 * Debug/verification entry point for the phone workspace.
 *
 * Run as shell uid through adb or Shizuku:
 * <pre>
 *   CLASSPATH=/data/local/tmp/workspace-probe.jar app_process / cn.xiaoyu.companion.WorkspaceShell
 * </pre>
 * It prints one JSON object per line on stdout and reads the same on stdin, so
 * a desktop session can drive a real display without the app. The app itself
 * uses {@link WorkspaceUserService} instead; both share {@link WorkspaceDisplay}.
 */
public final class WorkspaceShell {
    private WorkspaceShell() {}

    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(System.out, true);
        PrintStream log = System.err;
        int width = 0;
        int height = 0;
        int dpi = 0;
        for (String arg : args) {
            if (arg.startsWith("w=")) width = Integer.parseInt(arg.substring(2));
            else if (arg.startsWith("h=")) height = Integer.parseInt(arg.substring(2));
            else if (arg.startsWith("dpi=")) dpi = Integer.parseInt(arg.substring(4));
        }
        if (width <= 0 || height <= 0 || dpi <= 0) {
            reply(out, new JSONObject().put("status", "failed").put("code", "workspace_bad_geometry")
                    .put("output", "工作区需要 w/h/dpi 参数。"));
            System.exit(2);
        }
        WorkspaceDisplay display = null;
        try {
            display = WorkspaceDisplay.start(width, height, dpi);
            log.println("[workspace] display " + display.displayId() + " " + width + "x" + height + "/" + dpi);
            log.flush();
            reply(out, new JSONObject().put("status", "completed").put("event", "ready")
                    .put("display_id", display.displayId()).put("width", width).put("height", height).put("dpi", dpi));

            BufferedReader input = new BufferedReader(new InputStreamReader(System.in));
            String line;
            while ((line = input.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;
                JSONObject request;
                try {
                    request = new JSONObject(line);
                } catch (Exception error) {
                    reply(out, new JSONObject().put("status", "failed").put("code", "workspace_bad_request")
                            .put("output", "工作区请求不是有效 JSON。"));
                    continue;
                }
                if ("close".equals(request.optString("op"))) {
                    JSONObject answer = new JSONObject().put("status", "completed")
                            .put("display_id", display.displayId()).put("closed", true);
                    if (!request.optString("id", "").isEmpty()) answer.put("id", request.optString("id"));
                    reply(out, answer);
                    break;
                }
                JSONObject answer;
                try {
                    answer = display.handle(request);
                } catch (Exception error) {
                    answer = new JSONObject().put("status", "failed").put("code", "workspace_error")
                            .put("display_id", display.displayId())
                            .put("output", error.getClass().getSimpleName() + ": " + error.getMessage());
                }
                reply(out, answer);
            }
        } catch (Throwable error) {
            log.println("[workspace] fatal " + error);
            try {
                reply(out, new JSONObject().put("status", "failed").put("code", "workspace_shell_crashed")
                        .put("output", "工作区进程异常：" + error.getClass().getName() + ": " + error.getMessage()));
            } catch (Exception ignored) {
            }
            System.exit(3);
        } finally {
            if (display != null) display.close();
        }
        log.println("[workspace] released");
        log.flush();
        System.exit(0);
    }

    private static void reply(PrintStream out, JSONObject value) {
        out.println(value.toString());
        out.flush();
    }
}
