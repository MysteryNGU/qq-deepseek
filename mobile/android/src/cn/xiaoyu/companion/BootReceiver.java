package cn.xiaoyu.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent){
        if(Api.paired(context))try{context.startForegroundService(new Intent(context,MessageService.class));}catch(RuntimeException ignored){}
    }
}
