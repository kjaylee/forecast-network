package xyz.eastsea.forecast

import android.net.Uri
import android.util.Base64
import androidx.lifecycle.lifecycleScope
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender
import com.solana.mobilewalletadapter.clientlib.ConnectionIdentity
import com.solana.mobilewalletadapter.clientlib.MobileWalletAdapter
import com.solana.mobilewalletadapter.clientlib.Solana
import com.solana.mobilewalletadapter.clientlib.TransactionResult
import kotlinx.coroutines.launch

/**
 * Message-signing-only bridge to the Mobile Wallet Adapter.
 *
 * The page never sees the wallet's keys or an auth token, only base58 addresses,
 * raw public keys and detached signatures for bytes it supplied. No transaction
 * method exists here on purpose: Forecast never moves funds.
 */
@CapacitorPlugin(name = "MobileWallet")
class MobileWalletPlugin : Plugin() {
    private lateinit var sender: ActivityResultSender
    private lateinit var adapter: MobileWalletAdapter
    private var authorized: List<Account> = emptyList()

    private data class Account(val address: String, val publicKey: ByteArray, val label: String?)

    override fun load() {
        // ActivityResultSender must be created before the activity starts; load() runs in onCreate.
        sender = ActivityResultSender(activity)
        adapter = MobileWalletAdapter(
            connectionIdentity = ConnectionIdentity(
                identityUri = Uri.parse(IDENTITY_URI),
                iconUri = Uri.parse(ICON_PATH),
                identityName = IDENTITY_NAME,
            ),
        )
        adapter.blockchain = Solana.Devnet
    }

    @PluginMethod
    fun authorize(call: PluginCall) {
        val chain = call.getString("chain") ?: "solana:devnet"
        if (chain != "solana:devnet") {
            call.reject("Only solana:devnet is supported", "ERROR_CHAIN_UNSUPPORTED")
            return
        }
        activity.lifecycleScope.launch {
            when (val result = adapter.connect(sender)) {
                is TransactionResult.Success -> {
                    authorized = result.authResult.accounts.map {
                        Account(Base58.encode(it.publicKey), it.publicKey, it.accountLabel)
                    }
                    call.resolve(JSObject().put("accounts", accountsJson()))
                }
                is TransactionResult.NoWalletFound ->
                    call.reject("No Mobile Wallet Adapter wallet is installed", "ERROR_WALLET_NOT_FOUND")
                is TransactionResult.Failure ->
                    call.reject(result.e.message ?: "Authorization failed", "ERROR_AUTHORIZATION_FAILED")
            }
        }
    }

    @PluginMethod
    fun signMessage(call: PluginCall) {
        val address = call.getString("address")
        val encoded = call.getString("message")
        val account = authorized.firstOrNull { it.address == address }
        if (account == null) {
            call.reject("Account is not authorized", "ERROR_AUTHORIZATION_FAILED")
            return
        }
        val message = try {
            Base64.decode(encoded, Base64.DEFAULT)
        } catch (e: IllegalArgumentException) {
            call.reject("Message must be base64", "ERROR_INVALID_MESSAGE")
            return
        }
        if (message.isEmpty() || message.size > MAX_MESSAGE_BYTES) {
            call.reject("Message size is out of range", "ERROR_INVALID_MESSAGE")
            return
        }
        activity.lifecycleScope.launch {
            val result = adapter.transact(sender) { _ ->
                signMessagesDetached(arrayOf(message), arrayOf(account.publicKey))
            }
            when (result) {
                is TransactionResult.Success -> {
                    val signed = result.payload.messages.firstOrNull()
                    val signature = signed?.signatures?.firstOrNull()
                    if (signed == null || signature == null || signature.size != 64 || !signed.message.contentEquals(message)) {
                        call.reject("Wallet returned an unexpected signing result", "ERROR_SIGNATURE_INVALID")
                    } else {
                        call.resolve(JSObject().put("signature", Base64.encodeToString(signature, Base64.NO_WRAP)))
                    }
                }
                is TransactionResult.NoWalletFound ->
                    call.reject("No Mobile Wallet Adapter wallet is installed", "ERROR_WALLET_NOT_FOUND")
                is TransactionResult.Failure ->
                    call.reject(result.e.message ?: "Signing failed", "ERROR_SIGNING_FAILED")
            }
        }
    }

    @PluginMethod
    fun deauthorize(call: PluginCall) {
        authorized = emptyList()
        activity.lifecycleScope.launch {
            // Best effort: the wallet may already have discarded the session.
            runCatching { adapter.disconnect(sender) }
            call.resolve(JSObject())
        }
    }

    private fun accountsJson(): JSArray {
        val array = JSArray()
        for (account in authorized) {
            array.put(
                JSObject()
                    .put("address", account.address)
                    .put("publicKey", Base64.encodeToString(account.publicKey, Base64.NO_WRAP))
                    .put("label", account.label ?: "Seeker wallet"),
            )
        }
        return array
    }

    companion object {
        const val IDENTITY_URI = "https://forecast.eastsea.xyz"
        const val ICON_PATH = "favicon.svg"
        const val IDENTITY_NAME = "Forecast"
        const val MAX_MESSAGE_BYTES = 4096
    }
}
