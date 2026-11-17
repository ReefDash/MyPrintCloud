package com.myprintcloud.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(ProgressPlugin.class);  // lock-screen print progress
        super.onCreate(savedInstanceState);
    }
}
