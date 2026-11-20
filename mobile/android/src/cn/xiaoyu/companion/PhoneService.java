package cn.xiaoyu.companion;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.Rect;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.util.LinkedHashSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Native phone actions. Enabled only by the user's Android Accessibility setting. */
public class PhoneService extends AccessibilityService {
    private static volatile PhoneService current;
    private static final ExecutorService network=Executors.newSingleThreadExecutor();
    private static final ExecutorService workspace=Executors.newSingleThreadExecutor();
    private static final Handler main=new Handler(Looper.getMainLooper());
    private static final String[] OPERATIONS={"info","screenshot","tap","swipe","text","back","home","recents","open_app","apps"};
    private static final String[] WORKSPACE_OPERATIONS={"workspace_open","workspace_status","workspace_close"};
    // 跟着消息连接的 20 秒节奏重报能力；失败最多重试 3 次，退避 2s/4s/8s。
    private static final long REGISTER_INTERVAL_MS=20000L;
    private static final int REGISTER_RETRIES=3;
    private static final long REGISTER_BACKOFF_MS=2000L;
    private final LinkedHashSet<String> seen=new LinkedHashSet<>();
    private String active="";
    private static Context appContext;
    static boolean enabled(){return current!=null;}
    static void refreshCapabilities(){
        Context context=current!=null?current:appContext;
        if(context!=null&&current!=null)register(context);
    }
    // 注册状态写到 prefs，MainActivity 的状态接口读它：卡在哪一步能直接看到。
    private static void recordRegister(Context app,String result,String detail){
        try{
            String reason=detail==null?"":(detail.length()>200?detail.substring(0,200):detail);
            Api.prefs(app).edit().putLong("register_at",System.currentTimeMillis())
                .putString("register_result",result).putString("register_detail",reason).apply();
        }catch(Throwable ignored){}
    }
    private static void scheduleNextRegister(Context app){
        main.postDelayed(()->{try{register(app);}catch(Throwable ignored){}},REGISTER_INTERVAL_MS);
    }
    // 基础能力先建好：工作区那几项是可选装饰，拿不到也不能拖垮整个注册。
    private static JSONArray baseOperations(){
        JSONArray ops=new JSONArray();
        for(String op:OPERATIONS)if(!op.equals("screenshot")||Build.VERSION.SDK_INT>=30)ops.put(op);
        return ops;
    }
    static void register(Context context){
        final Context app=context.getApplicationContext();
        appContext=app;
        if(!Api.paired(app)){recordRegister(app,"unpaired","没有 token，还没有和云端配对");return;}
        network.execute(()->{
            JSONObject phone=null;
            try{
                phone=new JSONObject().put("enabled",enabled()).put("operations",baseOperations())
                    .put("coordinate_space","physical_screen_pixels");
            }catch(Throwable error){
                recordRegister(app,"failed","build:"+error.getClass().getSimpleName()
                    +(error.getMessage()==null?"":": "+error.getMessage()));
                scheduleNextRegister(app);
                return;
            }
            // 下面三段各自兜底：任何一段炸了，只丢那一段字段，基础能力照常送出。
            boolean workspaceReady=false;
            try{workspaceReady=PhoneWorkspaceManager.available();}catch(Throwable error){
                recordRegister(app,"partial","workspace_available:"+error.getClass().getSimpleName());
            }
            if(workspaceReady){
                try{JSONArray ops=phone.getJSONArray("operations");
                    for(String op:WORKSPACE_OPERATIONS)ops.put(op);}catch(Throwable ignored){}
            }
            try{phone.put("workspace_state",PhoneWorkspaceManager.state());}catch(Throwable error){
                phone = phoneOpt(phone,"workspace_state","unavailable:"+error.getClass().getSimpleName());
            }
            try{phone.put("workspaces",PhoneWorkspaceManager.capabilities());}catch(Throwable error){
                phone = phoneOpt(phone,"workspaces",new JSONArray());
            }
            try{
                String workspaceError=PhoneWorkspaceManager.lastError();
                if(!workspaceReady&&!workspaceError.isEmpty())phone.put("workspace_error",workspaceError);
            }catch(Throwable ignored){}
            final String payload;
            try{
                payload=new JSONObject().put("capabilities",new JSONObject().put("phone",phone)).toString();
            }catch(Throwable error){
                recordRegister(app,"failed","payload:"+error.getClass().getSimpleName());
                scheduleNextRegister(app);
                return;
            }
            for(int attempt=0;attempt<REGISTER_RETRIES;attempt++){
                try{
                    Api.Response response=Api.request(app,"POST","/mobile/device",payload);
                    if(response.status==200){
                        recordRegister(app,"ok","attempt="+(attempt+1));
                        scheduleNextRegister(app);
                        return;
                    }
                    recordRegister(app,"http_"+response.status,"attempt="+(attempt+1)+" "
                        +truncate(response.body));
                }catch(Throwable error){
                    recordRegister(app,"failed","attempt="+(attempt+1)+" "
                        +error.getClass().getSimpleName()
                        +(error.getMessage()==null?"":": "+error.getMessage()));
                }
                if(attempt+1<REGISTER_RETRIES){
                    try{Thread.sleep(REGISTER_BACKOFF_MS<<attempt);}catch(InterruptedException stop){
                        Thread.currentThread().interrupt();break;
                    }
                }
            }
            scheduleNextRegister(app);
        });
    }
    private static JSONObject phoneOpt(JSONObject phone,String key,Object value){
        try{phone.put(key,value);}catch(Throwable ignored){}
        return phone;
    }
    private static String truncate(String value){
        if(value==null)return "";
        return value.length()>120?value.substring(0,120):value;
    }
    static void receive(Context context,JSONObject event){
        String id=event.optString("id");if(id.isEmpty())return;
        main.post(()->{
            PhoneService service=current;
            if(event.optString("type").equals("phone_cancel")){if(service!=null)service.finish(id,false,"Cancelled; any action already applied cannot be undone.",null);return;}
            if(service==null){report(context,id,false,"Android Accessibility service is not enabled.",null);return;}
            service.execute(id,event.optString("op"),event.optJSONObject("args"));
        });
    }
    private static void report(Context context,String id,boolean success,String output,String png){
        final Context app=context.getApplicationContext();
        network.execute(()->{try{
            JSONObject result=new JSONObject().put("status",success?"completed":"failed").put("output",output);
            if(png!=null)result.put("image_png",png);
            Api.request(app,"POST","/mobile/control-result",new JSONObject().put("id",id).put("result",result).toString());
        }catch(Exception ignored){}});
    }
    @Override protected void onServiceConnected(){
        current=this;
        try{JSONArray ids=new JSONArray(Api.prefs(this).getString("phone_seen","[]"));for(int i=0;i<ids.length();i++)seen.add(ids.getString(i));}catch(Exception ignored){}
        PhoneWorkspaceManager.connect(this,false);
        register(this);try{MainActivity.event(new JSONObject().put("type","phone_capability").put("enabled",true));}catch(Exception ignored){}
    }
    @Override public void onAccessibilityEvent(AccessibilityEvent event){}
    @Override public void onInterrupt(){interruptAll("Android interrupted the Accessibility service.");}

    // 用户从 App 里主动"停止手机操作"走这里；与 onInterrupt 共用同一个收尾入口。
    // 注意：finish 是 private，但 Java 的 private 是**类级**可见性，同类静态方法访问本类实例成员合法。
    static void interruptAll(String reason){
        PhoneService service=current;
        if(service==null||service.active.isEmpty())return;
        service.finish(service.active,false,reason,null);
    }
    @Override public void onDestroy(){
        if(!active.isEmpty())finish(active,false,"Accessibility service disconnected.",null);
        if(current==this)current=null;register(this);
        try{MainActivity.event(new JSONObject().put("type","phone_capability").put("enabled",false));}catch(Exception ignored){}
        super.onDestroy();
    }
    private void finish(String id,boolean ok,String output,String png){
        if(!id.equals(active))return;active="";report(this,id,ok,output,png);
    }
    private static void reportResult(Context context,String id,JSONObject result){
        final Context app=context.getApplicationContext();
        network.execute(()->{try{
            JSONObject body=new JSONObject().put("id",id).put("result",result);
            Api.request(app,"POST","/mobile/control-result",body.toString());
        }catch(Exception ignored){}});
    }
    private void finishRich(String id,JSONObject result){
        if(!id.equals(active))return;active="";reportResult(this,id,result);
    }
    private void execute(String id,String op,JSONObject args){
        if(seen.contains(id))return;
        seen.add(id);while(seen.size()>500)seen.remove(seen.iterator().next());
        // Commit before a physical action: process death must not replay it.
        Api.prefs(this).edit().putString("phone_seen",new JSONArray(seen).toString()).commit();
        if(!active.isEmpty()){report(this,id,false,"Another phone action is still in progress.",null);return;}
        active=id;
        if(args==null)args=new JSONObject();
        boolean workspaceTarget=PhoneWorkspaceManager.handles(op)
            &&(op.startsWith("workspace_")||"workspace".equals(args.optString("target")));
        main.postDelayed(()->finish(id,false,"Phone action timed out; it will not be replayed.",null),
            workspaceTarget?45000:20000);
        if(workspaceTarget){
            final String requestId=id;final String requestOp=op;final JSONObject requestArgs=args;
            workspace.execute(()->{
                final JSONObject result=PhoneWorkspaceManager.handle(this,requestOp,requestArgs);
                main.post(()->finishRich(requestId,result));
            });
            return;
        }
        try{
            switch(op){
                case "info":finish(id,true,inspect().toString(),null);break;
                case "apps":listApps(id);break;
                case "screenshot":screenshot(id);break;
                case "tap":gesture(id,args,false);break;
                case "swipe":gesture(id,args,true);break;
                case "text":setText(id,args);break;
                case "back":global(id,GLOBAL_ACTION_BACK);break;
                case "home":global(id,GLOBAL_ACTION_HOME);break;
                case "recents":global(id,GLOBAL_ACTION_RECENTS);break;
                case "open_app":{
                    String name=args.getString("package");Intent intent=getPackageManager().getLaunchIntentForPackage(name);
                    if(intent==null)throw new IllegalArgumentException("No launchable installed app for package "+name);
                    startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));finish(id,true,"Launch intent dispatched for "+name+"; use info or screenshot to inspect the result.",null);break;
                }
                default:throw new IllegalArgumentException("Unsupported phone operation: "+op);
            }
        }catch(Exception error){finish(id,false,error.getClass().getSimpleName()+": "+error.getMessage(),null);}
    }
    private DisplayMetrics metrics(){DisplayMetrics metrics=new DisplayMetrics();getSystemService(android.view.WindowManager.class).getDefaultDisplay().getRealMetrics(metrics);return metrics;}
    private JSONObject inspect() throws Exception{
        DisplayMetrics m=metrics();JSONArray nodes=new JSONArray();AccessibilityNodeInfo root=getRootInActiveWindow();String pkg="";
        if(root!=null){try{pkg=String.valueOf(root.getPackageName());walk(root,"0",nodes,0);}finally{root.recycle();}}
        return new JSONObject().put("package",pkg).put("width",m.widthPixels).put("height",m.heightPixels).put("coordinate_space","physical_screen_pixels").put("nodes",nodes).put("truncated",nodes.length()>=180);
    }
    private String label(CharSequence text){if(text==null)return "";String value=text.toString();return value.length()>240?value.substring(0,240):value;}
    private void walk(AccessibilityNodeInfo node,String path,JSONArray nodes,int depth) throws Exception{
        if(nodes.length()>=180||depth>18)return;
        if(node.isVisibleToUser()){
            Rect bounds=new Rect();node.getBoundsInScreen(bounds);
            JSONObject item=new JSONObject().put("node",path).put("id",node.getViewIdResourceName()).put("class",label(node.getClassName())).put("clickable",node.isClickable()).put("editable",node.isEditable()).put("focused",node.isFocused()).put("password",node.isPassword()).put("bounds",new JSONArray(new int[]{bounds.left,bounds.top,bounds.right,bounds.bottom}));
            if(!node.isPassword())item.put("text",label(node.getText())).put("description",label(node.getContentDescription()));
            nodes.put(item);
        }
        for(int i=0;i<node.getChildCount()&&nodes.length()<180;i++){AccessibilityNodeInfo child=node.getChild(i);if(child!=null){try{walk(child,path+"."+i,nodes,depth+1);}finally{child.recycle();}}}
    }
    private void global(String id,int action){boolean ok=performGlobalAction(action);finish(id,ok,ok?"Android accepted the global action; inspect the resulting screen.":"Android rejected the global action.",null);}
    /** 可启动应用清单：让她自己看世界上有什么，而不是猜包名。 */
    private void listApps(String id){
        try{
            Intent main=new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            java.util.List<android.content.pm.ResolveInfo> resolved=getPackageManager().queryIntentActivities(main,0);
            java.util.TreeMap<String,String> sorted=new java.util.TreeMap<>();
            for(android.content.pm.ResolveInfo info:resolved){
                if(info.activityInfo==null)continue;
                CharSequence label=info.loadLabel(getPackageManager());
                sorted.put(info.activityInfo.packageName,label==null?info.activityInfo.packageName:label.toString());
            }
            JSONArray rows=new JSONArray();
            for(java.util.Map.Entry<String,String> entry:sorted.entrySet()){
                rows.put(new JSONObject().put("package",entry.getKey()).put("name",entry.getValue()));
                if(rows.length()>=200)break;
            }
            finish(id,true,rows.toString(),null);
        }catch(Throwable error){
            finish(id,false,"读取应用清单失败："+error.getClass().getSimpleName(),null);
        }
    }
    private float coordinate(JSONObject args,String name,int maximum)throws Exception{double value=args.getDouble(name);if(Double.isNaN(value)||Double.isInfinite(value)||value<0||value>=maximum)throw new IllegalArgumentException(name+" outside physical screen bounds");return(float)value;}
    private void gesture(String id,JSONObject args,boolean swipe)throws Exception{
        DisplayMetrics m=metrics();Path path=new Path();path.moveTo(coordinate(args,swipe?"x1":"x",m.widthPixels),coordinate(args,swipe?"y1":"y",m.heightPixels));
        if(swipe)path.lineTo(coordinate(args,"x2",m.widthPixels),coordinate(args,"y2",m.heightPixels));
        long duration=swipe?Math.max(50,Math.min(3000,args.optLong("duration_ms",400))):80;
        GestureDescription gesture=new GestureDescription.Builder().addStroke(new GestureDescription.StrokeDescription(path,0,duration)).build();
        boolean accepted=dispatchGesture(gesture,new GestureResultCallback(){
            @Override public void onCompleted(GestureDescription result){finish(id,true,"Gesture completed in physical screen coordinates.",null);}
            @Override public void onCancelled(GestureDescription result){finish(id,false,"Android cancelled the gesture.",null);}
        },main);
        if(!accepted)finish(id,false,"Android rejected the gesture.",null);
    }
    private void setText(String id,JSONObject args)throws Exception{
        String text=args.getString("text");if(text.length()>10000)throw new IllegalArgumentException("Text exceeds 10000 characters");
        AccessibilityNodeInfo root=getRootInActiveWindow();if(root==null)throw new IllegalStateException("No accessible active window");
        AccessibilityNodeInfo focused=null;
        try{focused=root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);if(focused==null||!focused.isEditable())throw new IllegalStateException("Focus an editable field first");
            Bundle data=new Bundle();data.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,text);
            boolean ok=focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,data);finish(id,ok,ok?"Android accepted text replacement in the focused field.":"The focused field rejected text replacement.",null);
        }finally{if(focused!=null)focused.recycle();root.recycle();}
    }
    private void screenshot(String id){
        if(Build.VERSION.SDK_INT<30){finish(id,false,"Screenshots require Android 11 or newer.",null);return;}
        takeScreenshot(Display.DEFAULT_DISPLAY,getMainExecutor(),new TakeScreenshotCallback(){
            @Override public void onSuccess(ScreenshotResult result){
                if(!id.equals(active)){result.getHardwareBuffer().close();return;}
                try(HardwareBuffer hardware=result.getHardwareBuffer()){
                    Bitmap bitmap=Bitmap.wrapHardwareBuffer(hardware,result.getColorSpace());if(bitmap==null)throw new IllegalStateException("Screenshot unavailable");
                    Bitmap image=bitmap.copy(Bitmap.Config.ARGB_8888,false);bitmap.recycle();
                    try{if((long)image.getWidth()*image.getHeight()>16000000L)throw new IllegalArgumentException("Screenshot exceeds 16 million pixels");
                        ByteArrayOutputStream bytes=new ByteArrayOutputStream();image.compress(Bitmap.CompressFormat.PNG,100,bytes);
                        if(bytes.size()>8*1024*1024)throw new IllegalArgumentException("Screenshot PNG exceeds 8 MiB");
                        finish(id,true,"Physical screen PNG "+image.getWidth()+"x"+image.getHeight()+"; coordinates are unscaled pixels.",Base64.encodeToString(bytes.toByteArray(),Base64.NO_WRAP));
                    }finally{image.recycle();}
                }catch(Exception error){finish(id,false,"Screenshot failed: "+error.getMessage(),null);}
            }
            @Override public void onFailure(int code){finish(id,false,"Android screenshot error "+code+" (protected windows or screenshot rate limits may apply).",null);}
        });
    }
}
