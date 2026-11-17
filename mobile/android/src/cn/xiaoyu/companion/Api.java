package cn.xiaoyu.companion;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class Api {
    static final String SERVER="https://47.104.8.84";
    static SharedPreferences prefs(Context context) { return context.getSharedPreferences("companion",Context.MODE_PRIVATE); }
    static boolean paired(Context context) { return !prefs(context).getString("token","").isEmpty(); }
    static String deviceId(Context context) { return prefs(context).getString("device_id",""); }
    static HttpURLConnection open(Context context,String method,String path) throws Exception {
        if (!path.startsWith("/mobile/") || path.contains("..") || path.contains("\\") || path.contains("#")) throw new IllegalArgumentException("invalid_path");
        HttpURLConnection connection=(HttpURLConnection)new URL(SERVER+path).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setRequestMethod(method);connection.setConnectTimeout(15000);connection.setReadTimeout(45000);
        String token=prefs(context).getString("token","");
        if (!token.isEmpty()) connection.setRequestProperty("Authorization","Bearer "+token);
        return connection;
    }
    static byte[] read(InputStream stream,int limit) throws Exception {
        if (stream==null) return new byte[0];
        try(InputStream input=stream;ByteArrayOutputStream output=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[16384];int count;
            while((count=input.read(buffer))!=-1) { if(output.size()+count>limit)throw new IllegalArgumentException("response_too_large");output.write(buffer,0,count); }
            return output.toByteArray();
        }
    }
    static final class Response { final int status;final String body;Response(int s,String b){status=s;body=b;} }
    static Response request(Context context,String method,String path,String body) throws Exception {
        return request(context,method,path,body,null);
    }
    static Response request(Context context,String method,String path,String body,java.util.function.Consumer<HttpURLConnection> opened) throws Exception {
        HttpURLConnection connection=open(context,method,path);
        if(opened!=null)opened.accept(connection);
        try {
            if (!method.equals("GET") && !method.equals("DELETE")) {
                byte[] raw=body.getBytes(StandardCharsets.UTF_8);connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type","application/json");connection.setFixedLengthStreamingMode(raw.length);
                try(java.io.OutputStream stream=connection.getOutputStream()){stream.write(raw);}
            }
            int status=connection.getResponseCode();
            byte[] raw=read(status<400?connection.getInputStream():connection.getErrorStream(),32*1024*1024);
            if (path.startsWith("/mobile/image/") && status==200) {
                org.json.JSONObject image=new org.json.JSONObject();image.put("mime",connection.getContentType());image.put("base64",android.util.Base64.encodeToString(raw,android.util.Base64.NO_WRAP));
                return new Response(status,image.toString());
            }
            String result=new String(raw,StandardCharsets.UTF_8);
            if(status==200&&path.equals("/mobile/state")){
                // Upgrade already-paired installations without making the user pair again.
                try{
                    org.json.JSONObject device=new org.json.JSONObject(result).optJSONObject("mobile_device");
                    if(device!=null&&!device.optString("id").isEmpty())prefs(context).edit().putString("device_id",device.getString("id")).apply();
                }catch(org.json.JSONException ignored){}
            }
            return new Response(status,result);
        } finally { connection.disconnect(); }
    }
}
