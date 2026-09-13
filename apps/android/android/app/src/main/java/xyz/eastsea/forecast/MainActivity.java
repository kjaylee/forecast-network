package xyz.eastsea.forecast;

import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.drawable.Drawable;
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
    private static final int TAB_BAR_BACKGROUND = Color.parseColor("#ffffff");

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(MobileWalletPlugin.class);
        super.onCreate(savedInstanceState);
        keepPageClearOfSystemBars();
        ResultNotifications.INSTANCE.ensureChannel(this);
        ResultNotifications.INSTANCE.requestPermission(this);
        ResultNotifications.INSTANCE.schedule(this);
    }

    @Override
    public void onStop() {
        super.onStop();
        // Leaving the app is the moment a result notification becomes useful.
        ResultNotifications.INSTANCE.checkNow(this);
    }

    /**
     * Android 15+ draws edge-to-edge; the page has no safe-area handling of its own, so the
     * WebView is inset by the status and navigation bars and the bars are painted to match
     * the page background with dark icons.
     */
    private void keepPageClearOfSystemBars() {
        View webView = getBridge().getWebView();
        View root = getWindow().getDecorView();
        // Top strip continues the page ground; bottom strip continues the tab bar surface.
        final SystemBarBackground bars = new SystemBarBackground(PAGE_BACKGROUND, TAB_BAR_BACKGROUND);
        root.setBackground(bars);
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(getWindow(), root);
        controller.setAppearanceLightStatusBars(true);
        controller.setAppearanceLightNavigationBars(true);
        ViewCompat.setOnApplyWindowInsetsListener(webView, (view, insets) -> {
            Insets insetsBars = insets.getInsets(WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());
            Insets ime = insets.getInsets(WindowInsetsCompat.Type.ime());
            ViewGroup.MarginLayoutParams params = (ViewGroup.MarginLayoutParams) view.getLayoutParams();
            params.topMargin = insetsBars.top;
            params.bottomMargin = Math.max(insetsBars.bottom, ime.bottom);
            params.leftMargin = insetsBars.left;
            params.rightMargin = insetsBars.right;
            view.setLayoutParams(params);
            // The theme may have swapped the decor background back to a plain colour; keep ours.
            if (root.getBackground() != bars) root.setBackground(bars);
            bars.setSplit(root.getHeight() - insetsBars.bottom);
            return WindowInsetsCompat.CONSUMED;
        });
        ViewCompat.requestApplyInsets(webView);
    }

    /** Two flat colours: page ground above the split line, tab-bar surface below it. */
    private static final class SystemBarBackground extends Drawable {
        private final Paint top = new Paint();
        private final Paint bottom = new Paint();
        private int split = Integer.MAX_VALUE;

        SystemBarBackground(int topColor, int bottomColor) {
            top.setColor(topColor);
            bottom.setColor(bottomColor);
        }

        void setSplit(int y) {
            split = y;
            invalidateSelf();
        }

        @Override
        public void draw(Canvas canvas) {
            int width = getBounds().width(), height = getBounds().height();
            int line = Math.min(Math.max(split, 0), height);
            canvas.drawRect(0, 0, width, line, top);
            canvas.drawRect(0, line, width, height, bottom);
        }

        @Override public void setAlpha(int alpha) {}
        @Override public void setColorFilter(ColorFilter filter) {}
        @Override public int getOpacity() { return PixelFormat.OPAQUE; }
    }
}
