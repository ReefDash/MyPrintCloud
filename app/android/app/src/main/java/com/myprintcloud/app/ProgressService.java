package com.myprintcloud.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.text.DateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.List;

/** Lock-screen progress: an ongoing notification that polls the server's /api/status while a print runs,
 *  and goes away by itself when the printer is no longer busy. */
public class ProgressService extends Service {
    static final String PREFS = "progress";
    private static final String CHANNEL = "print_progress";
    private static final int ID = 7101;
    private static final long EVERY_MS = 30_000;
    private static final long PIC_EVERY_MS = 60_000;  // camera picture on the notification
    private static final long GIVE_UP_MS = 30 * 60_000;  // server unreachable this long: stop trying
    private static final List<String> ACTIVE = Arrays.asList("PRINTING", "PAUSED", "PAUSING", "RESUMING", "HEATING",
            "PREPARING", "HOMING", "LEVELING", "FILAMENT SWAP");

    private HandlerThread thread;
    private Handler handler;
    private long lastOk, startedAt;
    private boolean sawActive;
    private Bitmap pic;
    private long picAt;

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Print progress", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Follows the running print on the lock screen");
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        thread = new HandlerThread("progress");
        thread.start();
        handler = new Handler(thread.getLooper());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && "stop".equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        Notification n = build("MyPrintCloud", "Checking the printer…", -1);
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ID, n);
        }
        lastOk = startedAt = System.currentTimeMillis();
        sawActive = false;
        pic = null;
        picAt = 0;
        handler.removeCallbacksAndMessages(null);
        handler.post(this::poll);
        return START_NOT_STICKY;
    }

    private void poll() {
        SharedPreferences p = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String server = p.getString("server", ""), token = p.getString("token", "");
        try {
            JSONObject s = get(server + "/api/status", token);
            lastOk = System.currentTimeMillis();
            JSONObject pr = s.optJSONObject("printer");
            String state = pr == null ? "" : pr.optString("state", "");
            if (!s.optBoolean("bridge") || !ACTIVE.contains(state)) {
                // finished, stopped or the printer went quiet: the phone alert covers the ending
                // (a print the app just sent can take a few minutes to upload and start)
                if (sawActive || !s.optBoolean("bridge") || System.currentTimeMillis() - startedAt > 10 * 60_000) { stopSelf(); return; }
                notify(build("MyPrintCloud", "Waiting for the print to start…", -1));
            } else {
                sawActive = true;
                if (System.currentTimeMillis() - picAt >= PIC_EVERY_MS) refreshPic(server, token);
                show(pr, s.optDouble("eta_factor", 1.0));
            }
        } catch (Exception e) {
            if (System.currentTimeMillis() - lastOk > GIVE_UP_MS) { stopSelf(); return; }
            notify(build("MyPrintCloud", "Can't reach the printer right now, still trying", -1));
        }
        handler.postDelayed(this::poll, EVERY_MS);
    }

    private void show(JSONObject pr, double factor) {
        String file = pr.optString("filename", "").replaceAll("\\.gcode$", "");
        int pct = pr.optInt("progress", 0), layer = pr.optInt("layer", 0), total = pr.optInt("total_layers", 0);
        String state = pr.optString("state", "");
        String head = "PRINTING".equals(state) ? pct + "% · " + file : nice(state) + " · " + file;
        StringBuilder b = new StringBuilder("Layer ").append(layer);
        if (total > 0) b.append(" of ").append(total);
        double rem = pr.optDouble("remaining_s", 0) * (factor > 0 ? factor : 1);
        if (rem > 0) {
            String at = DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date(System.currentTimeMillis() + (long) (rem * 1000)));
            b.append(" · done ≈ ").append(at);
        }
        notify(build(head, b.toString(), pct));
    }

    /** A recent camera frame (the server's failure-watch one, so this doesn't start the live stream). */
    private void refreshPic(String server, String token) {
        picAt = System.currentTimeMillis();
        try {
            byte[] jpg = fetch(server + "/api/cam.jpg?peek=1", token);
            Bitmap b = BitmapFactory.decodeByteArray(jpg, 0, jpg.length);
            if (b != null && b.getWidth() > 640) b = Bitmap.createScaledBitmap(b, 640, b.getHeight() * 640 / b.getWidth(), true);
            if (b != null) pic = b;
        } catch (Exception ignored) {  // keep the last picture
        }
    }

    private static String nice(String state) {
        switch (state) {
            case "PAUSED": return "Paused";
            case "HEATING": return "Heating";
            case "PREPARING": return "Preparing";
            case "FILAMENT SWAP": return "Changing filament";
            default: return state.isEmpty() ? "Busy" : state.charAt(0) + state.substring(1).toLowerCase();
        }
    }

    private Notification build(String title, String text, int pct) {
        Intent open = getPackageManager().getLaunchIntentForPackage(getPackageName());
        PendingIntent tap = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent hide = PendingIntent.getService(this, 1, new Intent(this, ProgressService.class).setAction("stop"),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        NotificationCompat.Builder nb = new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_print)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(tap)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setCategory(NotificationCompat.CATEGORY_PROGRESS)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .addAction(0, "Hide", hide);
        if (pct >= 0) nb.setProgress(100, pct, false);
        if (pct >= 0 && pic != null) {  // small next to the text, big when the notification is expanded
            nb.setLargeIcon(pic).setStyle(new NotificationCompat.BigPictureStyle().bigPicture(pic)
                    .bigLargeIcon((Bitmap) null).setSummaryText(text));
        }
        return nb.build();
    }

    private void notify(Notification n) {
        getSystemService(NotificationManager.class).notify(ID, n);
    }

    private static JSONObject get(String url, String token) throws Exception {
        return new JSONObject(new String(fetch(url, token), "UTF-8"));
    }

    private static byte[] fetch(String url, String token) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15_000);
        c.setReadTimeout(15_000);
        c.setRequestProperty("Authorization", "Bearer " + token);
        try {
            if (c.getResponseCode() != 200) throw new Exception("HTTP " + c.getResponseCode());
            InputStream in = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            for (int k; (k = in.read(buf)) > 0; ) out.write(buf, 0, k);
            return out.toByteArray();
        } finally {
            c.disconnect();
        }
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        thread.quitSafely();
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
