package xyz.eastsea.forecast;

import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    private static final int PAGE_BACKGROUND = Color.parseColor("#f7f8fb");

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(MobileWalletPlugin.class);
        super.onCreate(savedInstanceState);
        keepPageClearOfSystemBars();
    }

    /**
     * Android 15+ draws edge-to-edge; the page has no safe-area handling of its own, so the
     * WebView is inset by the status and navigation bars and the bars are painted to match
     * the page background with dark icons.
     */
    private void keepPageClearOfSystemBars() {
        View webView = getBridge().getWebView();
        View root = getWindow().getDecorView();
        root.setBackgroundColor(PAGE_BACKGROUND);
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), root);
        controller.setAppearanceLightStatusBars(true);
        controller.setAppearanceLightNavigationBars(true);
        ViewCompat.setOnApplyWindowInsetsListener(webView, (view, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            ViewGroup.MarginLayoutParams params = (ViewGroup.MarginLayoutParams) view.getLayoutParams();
            params.topMargin = bars.top;
            params.bottomMargin = Math.max(bars.bottom, ime.bottom);
            params.leftMargin = bars.left;
            params.rightMargin = bars.right;
            view.setLayoutParams(params);
            return WindowInsetsCompat.CONSUMED;
        });
        ViewCompat.requestApplyInsets(webView);
    }
}
