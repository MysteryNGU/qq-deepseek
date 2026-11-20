package cn.xiaoyu.companion;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;

public class MessageService extends Service {
    private volatile boolean alive=false;private Thread reader;private volatile HttpURLConnection connection;
    private final LinkedHashSet<String> seen=new LinkedHashSet<>();private NotificationManager notifications;
    // 手机工作区（Shizuku 副屏）保活：App 界面关掉、开机自启、系统杀进程后都靠这个线程重建。
    private volatile boolean workspaceAlive=true;private Thread workspaceKeepalive;
    @Override public void onCreate(){
        super.onCreate();notifications=getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("connection","保持消息连接",NotificationManager.IMPORTANCE_LOW));
        notifications.createNotificationChannel(new NotificationChannel("messages","小鱼的新消息",NotificationManager.IMPORTANCE_DEFAULT));
        try{JSONArray values=new JSONArray(Api.prefs(this).getString("seen","[]"));for(int i=0;i<values.length();i++)seen.add(values.getString(i));}catch(Exception ignored){}
        Notification notification=connectionNotification("正在连接小鱼…");
        if(Build.VERSION.SDK_INT>=34)startForeground(1,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING);else startForeground(1,notification);
        startWorkspaceKeepalive();
    }
    /**
     * 让手机工作区不依赖 App 界面：开机、界面被划掉、Shizuku 晚一点才启动，
     * 都由这里每 30 秒重试一次绑定；成功一次就重报能力，云端马上能看到手机。
     * 绑定是阻塞调用（最多等 3 秒），只能在后台线程跑。
     */
    private void startWorkspaceKeepalive(){
        if(workspaceKeepalive!=null)return;
        workspaceKeepalive=new Thread(()->{
            while(workspaceAlive){
                try{
                    if(Api.paired(MessageService.this)&&!PhoneWorkspaceManager.available()){
                        boolean ok=PhoneWorkspaceManager.connect(getApplicationContext(),false);
                        try{Api.prefs(MessageService.this).edit().putString("workspace_state",
                            new JSONObject().put("at",System.currentTimeMillis()).put("available",ok)
                                .put("state",PhoneWorkspaceManager.state())
                                .put("detail",PhoneWorkspaceManager.lastError()==null?"":PhoneWorkspaceManager.lastError())
                                .toString()).apply();}catch(Exception ignored){}
                        if(ok)try{PhoneService.refreshCapabilities();}catch(Throwable ignored){}
                    }
                }catch(Throwable ignored){}
                // 注册也要常驻：无障碍服务被系统回收时，这里仍按真实状态上报
                // （enabled=false 也是事实），她那边就不会凭空看到"手机离线"。
                if(Api.paired(MessageService.this))try{PhoneService.register(getApplicationContext());}catch(Throwable ignored){}
                try{Thread.sleep(30000L);}catch(InterruptedException stop){return;}
            }
        },"workspace-keepalive");
        workspaceKeepalive.setDaemon(true);
        workspaceKeepalive.start();
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(!Api.paired(this)){stopSelf();return START_NOT_STICKY;}
        if(!alive){alive=true;reader=new Thread(this::listen,"companion-events");reader.start();}
        startWorkspaceKeepalive();
        return START_STICKY;
    }
    @Override public IBinder onBind(Intent intent){return null;}
    private PendingIntent openApp(){return PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP|Intent.FLAG_ACTIVITY_CLEAR_TOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);}
    private Notification connectionNotification(String text){return new Notification.Builder(this,"connection").setSmallIcon(android.R.drawable.ic_dialog_email).setContentTitle("小鱼 · 消息连接").setContentText(text).setOngoing(true).setContentIntent(openApp()).build();}
    private void state(boolean connected){
        notifications.notify(1,connectionNotification(connected?"可以接收小鱼的消息":"连接已断开，正在重连"));
        try{MainActivity.event(new JSONObject().put("type","connection").put("connected",connected));}catch(Exception ignored){}
    }
    private void remember(String id){
        seen.add(id);while(seen.size()>500)seen.remove(seen.iterator().next());
        Api.prefs(this).edit().putString("seen",new JSONArray(seen).toString()).apply();
    }
    private boolean addressedHere(JSONObject event){
        if(!event.has("delivery")||event.isNull("delivery"))return true;
        JSONObject delivery=event.optJSONObject("delivery");
        JSONArray targets=delivery==null?null:delivery.optJSONArray("targets");
        if(targets==null)return false;
        String deviceId=Api.deviceId(this);
        for(int i=0;i<targets.length();i++){
            String target=targets.optString(i);
            if(target.equals("all")||(!deviceId.isEmpty()&&target.equals("mobile:"+deviceId)))return true;
        }
        return false;
    }
    private void deliver(JSONObject event){
        String type=event.optString("type");
        if(type.equals("phone_control")||type.equals("phone_cancel")){PhoneService.receive(this,event);return;}
        if(type.equals("ready"))PhoneService.register(this);
        boolean assistant=event.optString("type").equals("message")&&event.optString("role").equals("assistant");
        String id=event.optString("id");
        if(assistant&&!id.isEmpty()){
            boolean received=seen.contains(id);
            if(received){try{event.put("previously_received",true);}catch(Exception ignored){}}else remember(id);
            // All destinations share history; only this destination gets an alert.
            if(!received && addressedHere(event) && !MainActivity.foreground() && (Build.VERSION.SDK_INT<33||checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)==PackageManager.PERMISSION_GRANTED)){
                String text=event.optString("text",event.optString("content",""));if(text.isEmpty())text="发来了一张图片";
                if(text.length()>500)text=text.substring(0,500);
                Notification message=new Notification.Builder(this,"messages").setSmallIcon(android.R.drawable.ic_dialog_email).setContentTitle("小鱼")
                    .setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text)).setAutoCancel(true).setContentIntent(openApp()).build();
                notifications.notify(2+(id.hashCode()&0x7fffffff)%1000000000,message);
            }
        }
        MainActivity.event(event);
    }
    private void catchup() throws Exception {
        Api.Response response=Api.request(this,"GET","/mobile/state","");if(response.status!=200)throw new IllegalStateException("state_unavailable");
        JSONArray history=new JSONObject(response.body).optJSONArray("history");if(history==null)return;
        boolean first=!Api.prefs(this).getBoolean("seeded",false);
        for(int i=0;i<history.length();i++){
            JSONObject message=history.getJSONObject(i);if(!message.optString("role").equals("assistant"))continue;
            String id=message.optString("id");if(id.isEmpty())continue;
            if(first)remember(id);else{message.put("type","message");message.put("text",message.optString("content",message.optString("text")));deliver(message);}
        }
        Api.prefs(this).edit().putBoolean("seeded",true).apply();
    }
    private void listen(){
        while(alive){
            try{
                connection=Api.open(this,"GET","/mobile/events");connection.setRequestProperty("Accept","text/event-stream");
                int code=connection.getResponseCode();if(code!=200)throw new IllegalStateException("events_unavailable");
                // Establish SSE first: messages arriving during catch-up remain buffered.
                catchup();state(true);
                try(BufferedReader stream=new BufferedReader(new InputStreamReader(connection.getInputStream(),StandardCharsets.UTF_8))){
                    String line;StringBuilder data=new StringBuilder();
                    while(alive&&(line=stream.readLine())!=null){
                        if(line.isEmpty()){
                            if(data.length()>0){try{deliver(new JSONObject(data.toString()));}catch(Exception ignored){}data.setLength(0);}
                        }else if(line.startsWith("data:")){
                            if(data.length()>2*1024*1024)throw new IllegalStateException("event_too_large");
                            if(data.length()>0)data.append('\n');data.append(line.substring(5).trim());
                        }
                    }
                }
            }catch(Exception ignored){}finally{if(connection!=null){connection.disconnect();connection=null;}}
            if(alive){state(false);try{Thread.sleep(3000);}catch(InterruptedException ignored){}}
        }
    }
    @Override public void onDestroy(){alive=false;workspaceAlive=false;if(connection!=null)connection.disconnect();if(reader!=null)reader.interrupt();if(workspaceKeepalive!=null)workspaceKeepalive.interrupt();super.onDestroy();}
}
