package com.myprintcloud.app;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/** JS: Capacitor.registerPlugin("Progress").start({server, token}) / .stop() */
@CapacitorPlugin(name = "Progress")
public class ProgressPlugin extends Plugin {
    @PluginMethod
    public void start(PluginCall call) {
        Context ctx = getContext();
        ctx.getSharedPreferences(ProgressService.PREFS, Context.MODE_PRIVATE).edit()
                .putString("server", call.getString("server", ""))
                .putString("token", call.getString("token", ""))
                .apply();
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(getActivity(), new String[]{Manifest.permission.POST_NOTIFICATIONS}, 7101);
        }
        try {
            ContextCompat.startForegroundService(ctx, new Intent(ctx, ProgressService.class));
            call.resolve();
        } catch (Exception e) {  // e.g. Android refusing a start while the app is in the background
            call.reject(e.getMessage());
        }
    }

    @PluginMethod
    public void stop(PluginCall call) {
        getContext().stopService(new Intent(getContext(), ProgressService.class));
        call.resolve();
    }
}
