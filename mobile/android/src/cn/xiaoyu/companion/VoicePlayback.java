package cn.xiaoyu.companion;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Base64;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.net.HttpURLConnection;
import java.util.ArrayDeque;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.BiConsumer;

/** One ordered playback queue for automatic and manual speech, with immediate cancellation. */
final class VoicePlayback {
    private final Context context;private final BiConsumer<String,JSONObject> emit;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService network=Executors.newSingleThreadExecutor();
    private final ArrayDeque<String> queue=new ArrayDeque<>();
    private final AudioManager audio;private final AudioFocusRequest focus;
    private TextToSpeech tts;private boolean ttsReady,busy,closed;private volatile int generation;
    private MediaPlayer player;private File audioFile;private String utterance="";
    private volatile HttpURLConnection connection;
    private final AudioAttributes attributes=new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build();
    VoicePlayback(Context context,BiConsumer<String,JSONObject> emit){
        this.context=context;this.emit=emit;audio=context.getSystemService(AudioManager.class);
        focus=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK).setAudioAttributes(attributes).setOnAudioFocusChangeListener(change->{if(change==AudioManager.AUDIOFOCUS_LOSS||change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT)main.post(this::stop);}).build();
        tts=new TextToSpeech(context,status->{if(status==TextToSpeech.SUCCESS)ttsReady=tts.setLanguage(Locale.SIMPLIFIED_CHINESE)>=TextToSpeech.LANG_AVAILABLE;});
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener(){
            @Override public void onStart(String id){}
            @Override public void onDone(String id){main.post(()->{if(id.equals(utterance))done();});}
            @Override public void onError(String id){main.post(()->{if(id.equals(utterance)){state("error","手机中文语音播放失败。",true);done();}});}
        });
    }
    private void state(String state,String message,boolean fallback){try{emit.accept("onSpeechState",new JSONObject().put("state",state).put("message",message).put("fallback",fallback));}catch(Exception ignored){}}
    void say(String text){if(closed||text==null||text.trim().isEmpty())return;queue.add(text.trim());if(!busy)next();}
    private void next(){
        if(closed||queue.isEmpty()){busy=false;state("idle","",false);return;}
        busy=true;String text=queue.remove();int token=++generation;state("loading","正在准备语音…",false);
        network.execute(()->{
            if(token!=generation||closed)return;
            try{
                Api.Response response=Api.request(context,"POST","/mobile/speech",new JSONObject().put("text",text).toString(),opened->{if(token==generation)connection=opened;});
                if(token!=generation)return;
                JSONObject result=new JSONObject(response.body);
                if(response.status!=200)throw new IllegalStateException(result.optString("error","云端语音返回 "+response.status));
                String mime=result.optString("mime");if(!mime.startsWith("audio/"))throw new IllegalStateException("云端返回的内容不是音频");
                byte[] raw=Base64.decode(result.getString("base64"),Base64.DEFAULT);
                if(raw.length==0||raw.length>16*1024*1024)throw new IllegalStateException("语音音频大小异常");
                File file=File.createTempFile("speech_",mime.contains("wav")?".wav":".mp3",context.getCacheDir());
                try(FileOutputStream output=new FileOutputStream(file)){output.write(raw);}
                main.post(()->{if(token!=generation||closed){file.delete();return;}play(file,text,token);});
            }catch(Exception error){main.post(()->{if(token==generation&&!closed)fallback(text,error.getMessage());});}
        });
    }
    private void play(File file,String text,int token){
        audioFile=file;
        try{
            if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED)throw new IllegalStateException("暂时无法获得音频播放权限");
            player=new MediaPlayer();player.setAudioAttributes(attributes);player.setDataSource(file.getAbsolutePath());
            player.setOnPreparedListener(ready->{if(token==generation){ready.start();state("playing","",false);}});
            player.setOnCompletionListener(finished->{if(token==generation)done();});
            player.setOnErrorListener((failed,what,extra)->{if(token==generation){releasePlayer();fallback(text,"云端音频无法播放（"+what+"）");}return true;});
            player.prepareAsync();
        }catch(Exception error){releasePlayer();fallback(text,error.getMessage());}
    }
    private void fallback(String text,String reason){
        state("error","云端语音失败："+(reason==null?"连接中断":reason)+(ttsReady?"；改用手机中文语音。":"；手机未安装可用的中文语音。"),ttsReady);
        if(!ttsReady){done();return;}
        if(audio.requestAudioFocus(focus)!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED){state("error","暂时无法获得音频播放权限。",true);done();return;}
        utterance=generation+":"+java.util.UUID.randomUUID();
        if(tts.speak(text,TextToSpeech.QUEUE_FLUSH,null,utterance)==TextToSpeech.ERROR){state("error","手机语音播放失败。",true);done();return;}
        state("playing","使用手机中文语音",true);
    }
    private void releasePlayer(){if(player!=null){player.release();player=null;}if(audioFile!=null){audioFile.delete();audioFile=null;}}
    private void done(){releasePlayer();utterance="";audio.abandonAudioFocusRequest(focus);busy=false;next();}
    void stop(){generation++;queue.clear();releasePlayer();utterance="";if(tts!=null)tts.stop();audio.abandonAudioFocusRequest(focus);busy=false;state("idle","",false);HttpURLConnection active=connection;connection=null;if(active!=null)active.disconnect();}
    void close(){closed=true;stop();if(tts!=null)tts.shutdown();network.shutdownNow();}
}
