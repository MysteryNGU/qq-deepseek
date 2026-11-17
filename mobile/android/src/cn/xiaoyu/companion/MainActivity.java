package cn.xiaoyu.companion;

import android.Manifest;
import android.app.Activity;
import android.content.ComponentName;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.provider.MediaStore;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.widget.FrameLayout;
import android.widget.Toast;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceResponse;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.lang.ref.WeakReference;
import java.util.LinkedHashSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static WeakReference<MainActivity> current=new WeakReference<>(null);
    private WebView view;private boolean visible=false;private VoicePlayback speech;private Uri cameraUri;
    private final ExecutorService work=Executors.newFixedThreadPool(3);
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if(saved!=null&&saved.getString("camera_uri")!=null)cameraUri=Uri.parse(saved.getString("camera_uri"));
        if(Build.VERSION.SDK_INT>=30){
            getWindow().setDecorFitsSystemWindows(false);
            getWindow().setStatusBarColor(android.graphics.Color.TRANSPARENT);
            getWindow().setNavigationBarColor(android.graphics.Color.TRANSPARENT);
        }
        FrameLayout content=new FrameLayout(this);
        content.setBackgroundColor(android.graphics.Color.WHITE);
        view=new WebView(this);
        content.addView(view,new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(content);
        if(Build.VERSION.SDK_INT>=30){
            WindowInsetsController controller=getWindow().getInsetsController();
            if(controller!=null)controller.setSystemBarsAppearance(
                WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS|WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS,
                WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS|WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
            content.setOnApplyWindowInsetsListener((v,insets)->{
                // The union takes the maximum on each edge: IME already overlaps the navigation bar.
                android.graphics.Insets safe=insets.getInsets(WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout()|WindowInsets.Type.ime());
                v.setPadding(safe.left,safe.top,safe.right,safe.bottom);
                // Resize the WebView's actual viewport; do not let it also apply IME insets internally.
                return WindowInsets.CONSUMED;
            });
            content.requestApplyInsets();
        }else{
            // Android 8-10: the decor already fits system bars and adjustResize handles the keyboard.
            getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        }
        view.getSettings().setJavaScriptEnabled(true);view.getSettings().setDomStorageEnabled(true);
        view.getSettings().setAllowFileAccess(false);view.getSettings().setAllowContentAccess(false);
        view.getSettings().setAllowFileAccessFromFileURLs(false);view.getSettings().setAllowUniversalAccessFromFileURLs(false);
        view.addJavascriptInterface(new Bridge(),"Native");
        view.setWebViewClient(new WebViewClient(){
            @Override public WebResourceResponse shouldInterceptRequest(WebView web,WebResourceRequest request){
                String address=request.getUrl().toString();
                if(address.startsWith("file:///android_asset/")||address.startsWith("data:"))return null;
                return new WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));
            }
            @Override public boolean shouldOverrideUrlLoading(WebView web,WebResourceRequest request) {
                Uri url=request.getUrl();
                if (url.toString().equals("file:///android_asset/index.html")) return false;
                if (url.getScheme()!=null && (url.getScheme().equals("https") || url.getScheme().equals("http"))) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW,url)); } catch(Exception ignored){}
                }
                return true;
            }
        });
        view.loadUrl("file:///android_asset/index.html");
        speech=new VoicePlayback(this,this::nativeEvent);
        if(Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},30);
        maybeRunWorkspaceSelfTest(getIntent());
    }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);maybeRunWorkspaceSelfTest(intent);}
    /**
     * Diagnostic only: adb can ask for one real workspace round-trip without going
     * through the cloud, so the device link can be verified on its own.
     *   adb shell am start -n cn.xiaoyu.companion/.MainActivity --activity-single-top      *     --es workspace_selftest com.android.settings
     */
    private void maybeRunWorkspaceSelfTest(Intent intent){
        if(intent==null||!intent.hasExtra("workspace_selftest"))return;
        final String target=intent.getStringExtra("workspace_selftest");
        new Thread(()->{
            try{
                final java.util.concurrent.CountDownLatch ready=new java.util.concurrent.CountDownLatch(1);
                runOnUiThread(()->{PhoneWorkspaceManager.connect(this,true);ready.countDown();});
                ready.await(5,java.util.concurrent.TimeUnit.SECONDS);
                JSONObject open=PhoneWorkspaceManager.handle(this,"workspace_open",
                    new JSONObject().put("package",target.isEmpty()?"com.android.settings":target));
                android.util.Log.i("XiaoyuWorkspace","selftest open: "+trim(open));
                Thread.sleep(4000);
                JSONObject identity=new JSONObject().put("target","workspace")
                    .put("workspace_id",open.optString("workspace_id"))
                    .put("session_id",open.optString("session_id"));
                JSONObject shot=PhoneWorkspaceManager.handle(this,"screenshot",identity);
                android.util.Log.i("XiaoyuWorkspace","selftest shot: "+trim(shot));
                String png=shot.optString("image_png","");
                if(!png.isEmpty()){
                    java.io.File folder=getExternalFilesDir(null);
                    if(folder!=null){
                        java.io.File file=new java.io.File(folder,"workspace-selftest.png");
                        try(java.io.FileOutputStream out=new java.io.FileOutputStream(file)){
                            out.write(android.util.Base64.decode(png,android.util.Base64.NO_WRAP));
                        }
                        android.util.Log.i("XiaoyuWorkspace","selftest png: "+file.getAbsolutePath()+" "+file.length());
                    }
                }
                JSONObject close=PhoneWorkspaceManager.handle(this,"workspace_close",identity);
                android.util.Log.i("XiaoyuWorkspace","selftest close: "+trim(close));
            }catch(Throwable error){
                android.util.Log.w("XiaoyuWorkspace","selftest failed",error);
            }
        },"workspace-selftest").start();
    }
    private static String trim(JSONObject value){
        if(value==null)return "null";
        String text=value.toString();
        return text.length()>300?text.substring(0,300)+"...":text;
    }
    @Override protected void onResume(){super.onResume();visible=true;current=new WeakReference<>(this);if(Api.paired(this)){startMessages();PhoneWorkspaceManager.connect(this,true);PhoneService.register(this);}script("window.onServerEvent&&window.onServerEvent({type:'ready'})");}
    @Override protected void onPause(){visible=false;super.onPause();}
    @Override protected void onDestroy(){visible=false;if(current.get()==this)current.clear();if(speech!=null)speech.close();work.shutdownNow();view.removeJavascriptInterface("Native");view.destroy();super.onDestroy();}
    @Override protected void onSaveInstanceState(Bundle state){super.onSaveInstanceState(state);if(cameraUri!=null)state.putString("camera_uri",cameraUri.toString());}
    static boolean foreground(){MainActivity activity=current.get();return activity!=null&&activity.visible;}
    static void event(JSONObject event){MainActivity activity=current.get();if(activity!=null&&activity.visible)activity.script("window.onServerEvent&&window.onServerEvent("+event.toString()+")");}
    private void script(String js){runOnUiThread(()->{if(!isFinishing()&&!isDestroyed())view.evaluateJavascript(js,null);});}
    private void nativeEvent(String callback,JSONObject value){script("window."+callback+"&&window."+callback+"("+value.toString()+")");}
    private void imageError(String message){try{nativeEvent("onPickedImage",new JSONObject().put("error",message));}catch(Exception ignored){}}
    private void startMessages(){try{startForegroundService(new Intent(this,MessageService.class));}catch(Exception ignored){}}
    private boolean openSetting(Intent intent){try{startActivity(intent);return true;}catch(Exception unavailable){return false;}}
    private void openAppSettings(){
        if(!openSetting(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())))
            && !openSetting(new Intent(Settings.ACTION_SETTINGS)))Toast.makeText(this,"无法打开系统设置，请从系统设置进入小鱼的应用详情。",Toast.LENGTH_LONG).show();
    }
    private void openPhoneSettings(){
        ComponentName service=new ComponentName(this,PhoneService.class);
        // AOSP defines this details action as SystemApi, so use its documented wire value.
        if(openSetting(new Intent("android.settings.ACCESSIBILITY_DETAILS_SETTINGS").putExtra(Intent.EXTRA_COMPONENT_NAME,service)))return;
        if(openSetting(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).putExtra(Intent.EXTRA_COMPONENT_NAME,service)))return;
        openAppSettings();
    }
    public final class Bridge {
        @JavascriptInterface public String config(){try{
            PackageInfo info=getPackageManager().getPackageInfo(getPackageName(),0);
            // 最近一次能力注册的尝试：结果/时间/失败原因，用来判断手机卡在哪一步。
            long registerAt=Api.prefs(MainActivity.this).getLong("register_at",0);
            return new JSONObject().put("server",Api.SERVER).put("paired",Api.paired(MainActivity.this)).put("phone_enabled",PhoneService.enabled())
                .put("device_id",Api.deviceId(MainActivity.this)).put("device_name",Build.MANUFACTURER+" "+Build.MODEL)
                .put("speech",Api.prefs(MainActivity.this).getBoolean("speech",true)).put("version",info.versionName)
                .put("version_code",Build.VERSION.SDK_INT>=28?info.getLongVersionCode():info.versionCode)
                .put("register_at",registerAt).put("register_age_ms",registerAt==0?-1:(System.currentTimeMillis()-registerAt))
                .put("register_result",Api.prefs(MainActivity.this).getString("register_result",""))
                .put("register_detail",Api.prefs(MainActivity.this).getString("register_detail","")).toString();
        }catch(Exception error){return "{}";}}
        @JavascriptInterface public void request(String id,String method,String path,String body){
            work.execute(()->{
                int status=0;String result;
                try {
                    if(!method.equals("GET")&&!method.equals("POST")&&!method.equals("DELETE"))throw new IllegalArgumentException("invalid_method");
                    Api.Response response=Api.request(MainActivity.this,method,path,body);status=response.status;result=response.body;
                    if(path.equals("/mobile/pair")&&status==200){
                        JSONObject paired=new JSONObject(result);
                        Api.prefs(MainActivity.this).edit().putString("token",paired.getString("token")).putString("device_id",paired.optString("device_id"))
                            .remove("seen").remove("seeded").apply();
                        runOnUiThread(()->stopService(new Intent(MainActivity.this,MessageService.class)));
                    }
                }catch(Exception error){result="{\"error\":\""+error.getClass().getSimpleName()+"\"}";}
                script("window.onNativeResult&&window.onNativeResult("+JSONObject.quote(id)+","+status+","+JSONObject.quote(result)+")");
            });
        }
        @JavascriptInterface public void startConnection(){runOnUiThread(()->startMessages());}
        @JavascriptInterface public void pickImage(){runOnUiThread(()->MainActivity.this.pickImages());}
        @JavascriptInterface public void takePhoto(){runOnUiThread(()->MainActivity.this.takePhoto());}
        @JavascriptInterface public void openSettings(){runOnUiThread(()->{try{startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));}catch(Exception error){startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,Uri.parse("package:"+getPackageName())));}});}
        @JavascriptInterface public void openPhoneSettings(){runOnUiThread(()->MainActivity.this.openPhoneSettings());}
        @JavascriptInterface public void openAppSettings(){runOnUiThread(()->MainActivity.this.openAppSettings());}
        @JavascriptInterface public void setSpeech(boolean enabled){Api.prefs(MainActivity.this).edit().putBoolean("speech",enabled).apply();if(!enabled)runOnUiThread(()->speech.stop());}
        @JavascriptInterface public void speak(String text){runOnUiThread(()->speech.say(text));}
        @JavascriptInterface public void stopSpeech(){runOnUiThread(()->speech.stop());}
    }
    private void pickImages(){
        Intent picker=new Intent("android.provider.action.PICK_IMAGES").setType("image/*");
        picker.putExtra("android.provider.extra.PICK_IMAGES_MAX",Build.VERSION.SDK_INT>=33?Math.min(8,MediaStore.getPickImagesMaxLimit()):8);
        try{startActivityForResult(picker,40);return;}catch(Exception unavailable){}
        try{startActivityForResult(new Intent(Intent.ACTION_PICK).setDataAndType(MediaStore.Images.Media.EXTERNAL_CONTENT_URI,"image/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE,true),40);return;}catch(Exception unavailable){}
        try{startActivityForResult(new Intent(Intent.ACTION_GET_CONTENT).setType("image/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE,true),40);}catch(Exception unavailable){imageError("手机没有可用的相册选择器。");}
    }
    private void takePhoto(){
        try{
            File image=new File(PhotoProvider.directory(this),"photo_"+java.util.UUID.randomUUID()+".jpg");
            if(!image.createNewFile())throw new IllegalStateException("无法准备照片文件");
            cameraUri=new Uri.Builder().scheme("content").authority(getPackageName()+".photos").appendPath(image.getName()).build();
            Intent camera=new Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT,cameraUri)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            camera.setClipData(ClipData.newRawUri("photo",cameraUri));startActivityForResult(camera,41);
        }catch(Exception error){if(cameraUri!=null)cleanPhoto(cameraUri);cameraUri=null;imageError("无法打开相机："+error.getMessage());}
    }
    private void cleanPhoto(Uri uri){
        revokeUriPermission(uri,Intent.FLAG_GRANT_READ_URI_PERMISSION|Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        new File(PhotoProvider.directory(this),uri.getLastPathSegment()).delete();
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request==41){
            Uri photo=cameraUri;cameraUri=null;if(photo==null)return;
            if(result!=RESULT_OK){cleanPhoto(photo);return;}
            work.execute(()->{try{processImage(photo);}finally{cleanPhoto(photo);}});return;
        }
        if(request!=40||result!=RESULT_OK||data==null)return;
        LinkedHashSet<Uri> selected=new LinkedHashSet<>();ClipData clips=data.getClipData();
        if(clips!=null)for(int i=0;i<Math.min(8,clips.getItemCount());i++)selected.add(clips.getItemAt(i).getUri());
        else if(data.getData()!=null)selected.add(data.getData());
        work.execute(()->{for(Uri uri:selected)processImage(uri);});
    }
    private void processImage(Uri uri){
            try{
                byte[] raw=Api.read(getContentResolver().openInputStream(uri),32*1024*1024);
                BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;BitmapFactory.decodeByteArray(raw,0,raw.length,bounds);
                if(bounds.outWidth<=0||bounds.outHeight<=0||((long)bounds.outWidth)*bounds.outHeight>64000000L)throw new IllegalArgumentException("invalid_image");
                BitmapFactory.Options options=new BitmapFactory.Options();options.inSampleSize=1;
                while(Math.max(bounds.outWidth,bounds.outHeight)/options.inSampleSize>3200)options.inSampleSize*=2;
                Bitmap image=BitmapFactory.decodeByteArray(raw,0,raw.length,options);if(image==null)throw new IllegalArgumentException("invalid_image");
                int orientation=ExifInterface.ORIENTATION_NORMAL;try{orientation=new ExifInterface(new ByteArrayInputStream(raw)).getAttributeInt(ExifInterface.TAG_ORIENTATION,ExifInterface.ORIENTATION_NORMAL);}catch(Exception ignored){}
                Matrix transform=new Matrix();if(orientation==6||orientation==5)transform.postRotate(90);else if(orientation==3||orientation==4)transform.postRotate(180);else if(orientation==8||orientation==7)transform.postRotate(270);
                if(orientation==2||orientation==4||orientation==5||orientation==7)transform.postScale(-1,1);
                if(!transform.isIdentity()){Bitmap rotated=Bitmap.createBitmap(image,0,0,image.getWidth(),image.getHeight(),transform,true);if(rotated!=image)image.recycle();image=rotated;}
                float scale=Math.min(1f,1600f/Math.max(image.getWidth(),image.getHeight()));
                if(scale<1){Bitmap resized=Bitmap.createScaledBitmap(image,Math.max(1,Math.round(image.getWidth()*scale)),Math.max(1,Math.round(image.getHeight()*scale)),true);image.recycle();image=resized;}
                boolean alpha=image.hasAlpha();ByteArrayOutputStream output=new ByteArrayOutputStream();image.compress(alpha?Bitmap.CompressFormat.PNG:Bitmap.CompressFormat.JPEG,88,output);image.recycle();
                if(output.size()>8*1024*1024)throw new IllegalArgumentException("image_too_large");
                JSONObject selected=new JSONObject().put("name",alpha?"photo.png":"photo.jpg").put("mime",alpha?"image/png":"image/jpeg").put("base64",Base64.encodeToString(output.toByteArray(),Base64.NO_WRAP));
                script("window.onPickedImage&&window.onPickedImage("+selected.toString()+")");
            }catch(Exception error){imageError("无法读取图片："+(error.getMessage()==null?error.getClass().getSimpleName():error.getMessage()));}
    }
}
