package cn.xiaoyu.companion;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;

/** Grants the camera access only to explicitly shared temporary JPEGs. */
public class PhotoProvider extends ContentProvider {
    @Override public boolean onCreate(){return true;}
    static File directory(android.content.Context context){File dir=new File(context.getCacheDir(),"camera");dir.mkdirs();return dir;}
    private File file(Uri uri)throws FileNotFoundException{
        String name=uri.getLastPathSegment();
        if(uri.getPathSegments().size()!=1||name==null||!name.matches("photo_[a-f0-9-]+\\.jpg"))throw new FileNotFoundException("Invalid photo URI");
        File file=new File(directory(getContext()),name);
        if(!file.isFile())throw new FileNotFoundException("Photo no longer exists");
        return file;
    }
    @Override public ParcelFileDescriptor openFile(Uri uri,String mode)throws FileNotFoundException{return ParcelFileDescriptor.open(file(uri),ParcelFileDescriptor.parseMode(mode));}
    @Override public String getType(Uri uri){return "image/jpeg";}
    @Override public Cursor query(Uri uri,String[] projection,String selection,String[] args,String order){
        try{File image=file(uri);String[] columns=projection==null?new String[]{OpenableColumns.DISPLAY_NAME,OpenableColumns.SIZE}:projection;
            MatrixCursor cursor=new MatrixCursor(columns);Object[] values=new Object[columns.length];
            for(int i=0;i<columns.length;i++)values[i]=OpenableColumns.DISPLAY_NAME.equals(columns[i])?image.getName():OpenableColumns.SIZE.equals(columns[i])?image.length():null;
            cursor.addRow(values);return cursor;
        }catch(FileNotFoundException error){return null;}
    }
    @Override public Uri insert(Uri uri,ContentValues values){throw new UnsupportedOperationException();}
    @Override public int update(Uri uri,ContentValues values,String where,String[] args){throw new UnsupportedOperationException();}
    @Override public int delete(Uri uri,String where,String[] args){throw new UnsupportedOperationException();}
}
