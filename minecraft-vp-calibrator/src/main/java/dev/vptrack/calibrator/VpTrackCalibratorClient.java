package dev.vptrack.calibrator;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.fabricmc.fabric.api.client.message.v1.ClientSendMessageEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.MinecraftClient;
import net.minecraft.text.Text;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.OptionalInt;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

public final class VpTrackCalibratorClient implements ClientModInitializer {
    private static final Logger LOGGER = LoggerFactory.getLogger("vptrack_calibrator");
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();
    private static final String CONFIG_FILE = "vptrack-calibrator.json";

    private Config config;
    private List<Pattern> messagePatterns = List.of();
    private volatile long listenUntilMillis = 0L;
    private volatile int lastCalibratedCount = -1;
    private volatile long lastCalibratedAtMillis = 0L;

    @Override
    public void onInitializeClient() {
        reloadConfig();

        ClientSendMessageEvents.COMMAND.register(command -> {
            if (isVotePartyCommand(command)) {
                reloadConfig();
            }
            if (config.enabled && isVotePartyCommand(command)) {
                armListener();
            }
        });

        ClientReceiveMessageEvents.CHAT.register((message, signedMessage, sender, params, receptionTimestamp) -> scanMessage(message));
        ClientReceiveMessageEvents.GAME.register((message, overlay) -> scanMessage(message));
        LOGGER.info("VPTrack calibrator initialized as a client-side listener.");
    }

    private void reloadConfig() {
        config = Config.load();
        messagePatterns = compilePatterns(config);
    }

    private boolean isVotePartyCommand(String command) {
        String normalized = command.trim().toLowerCase(Locale.ROOT);
        if (normalized.startsWith("/")) {
            normalized = normalized.substring(1);
        }
        return normalized.equals("voteparty") || normalized.startsWith("voteparty ");
    }

    private void armListener() {
        long windowMillis = Math.max(1, config.listenWindowSeconds) * 1000L;
        listenUntilMillis = System.currentTimeMillis() + windowMillis;
        LOGGER.debug("Armed Vote Party response listener for {} seconds.", config.listenWindowSeconds);
    }

    private boolean listenerIsArmed() {
        return System.currentTimeMillis() <= listenUntilMillis;
    }

    private void scanMessage(Text message) {
        if (!config.enabled || !listenerIsArmed()) {
            return;
        }
        String plain = message.getString();
        OptionalInt count = extractCount(plain);
        if (count.isEmpty()) {
            return;
        }

        int current = count.getAsInt();
        if (isDuplicate(current)) {
            return;
        }
        listenUntilMillis = 0L;
        lastCalibratedCount = current;
        lastCalibratedAtMillis = System.currentTimeMillis();
        runCalibration(current, plain);
    }

    private OptionalInt extractCount(String message) {
        for (Pattern pattern : messagePatterns) {
            Matcher matcher = pattern.matcher(message);
            if (!matcher.find()) {
                continue;
            }
            try {
                int count = Integer.parseInt(matcher.group(1));
                if (count >= 0 && count <= config.partySize) {
                    return OptionalInt.of(count);
                }
            } catch (NumberFormatException ignored) {
                return OptionalInt.empty();
            }
        }
        return OptionalInt.empty();
    }

    private boolean isDuplicate(int count) {
        long ageMillis = System.currentTimeMillis() - lastCalibratedAtMillis;
        return count == lastCalibratedCount
            && ageMillis >= 0
            && ageMillis < Math.max(0, config.dedupeSeconds) * 1000L;
    }

    private void runCalibration(int count, String rawMessage) {
        if (config.calibrationCommand == null || config.calibrationCommand.isEmpty()) {
            LOGGER.warn(
                "VPTrack calibrator found {}/{} but calibrationCommand is empty in {}.",
                count,
                config.partySize,
                Config.path()
            );
            showClientMessage(
                "VPTrack found " + count + "/" + config.partySize
                    + ", but no calibration command is configured."
            );
            return;
        }

        List<String> command = new ArrayList<>();
        for (String token : config.calibrationCommand) {
            command.add(token
                .replace("{count}", Integer.toString(count))
                .replace("{partySize}", Integer.toString(config.partySize))
                .replace("{message}", rawMessage));
        }

        CompletableFuture.runAsync(() -> {
            try {
                ProcessBuilder builder = new ProcessBuilder(command);
                if (config.workingDirectory != null && !config.workingDirectory.isBlank()) {
                    builder.directory(Path.of(config.workingDirectory).toFile());
                }
                builder.redirectErrorStream(true);
                Process process = builder.start();
                boolean finished = process.waitFor(Math.max(1, config.commandTimeoutSeconds), TimeUnit.SECONDS);
                if (!finished) {
                    process.destroyForcibly();
                    LOGGER.warn("VPTrack calibration command timed out after {} seconds.", config.commandTimeoutSeconds);
                    showClientMessage("VPTrack calibration timed out.");
                    return;
                }
                String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8).trim();
                if (process.exitValue() == 0) {
                    LOGGER.info("Calibrated local VP tracker to {}/{}.", count, config.partySize);
                    showClientMessage("VPTrack calibrated to " + count + "/" + config.partySize + ".");
                } else {
                    LOGGER.warn("VPTrack calibration command exited {}: {}", process.exitValue(), output);
                    showClientMessage("VPTrack calibration failed with exit " + process.exitValue() + ".");
                }
            } catch (IOException e) {
                LOGGER.warn("Could not run VPTrack calibration command.", e);
                showClientMessage("VPTrack calibration command could not start.");
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                LOGGER.warn("VPTrack calibration command was interrupted.", e);
            }
        });
    }

    private void showClientMessage(String message) {
        if (!config.showClientConfirmation) {
            return;
        }
        MinecraftClient client = MinecraftClient.getInstance();
        client.execute(() -> {
            if (client.inGameHud != null) {
                client.inGameHud.getChatHud().addMessage(Text.literal("[VPTrack] " + message));
            }
        });
    }

    private static List<Pattern> compilePatterns(Config config) {
        List<Pattern> compiled = new ArrayList<>();
        for (String regex : config.messagePatterns) {
            try {
                compiled.add(Pattern.compile(regex, Pattern.CASE_INSENSITIVE));
            } catch (PatternSyntaxException e) {
                LOGGER.warn("Ignoring invalid VPTrack message pattern: {}", regex, e);
            }
        }
        if (compiled.isEmpty()) {
            compiled.add(Pattern.compile("\\bvote\\s*party\\b.*?(\\d{1,3})\\s*/\\s*120\\b", Pattern.CASE_INSENSITIVE));
        }
        return compiled;
    }

    private static final class Config {
        boolean enabled = true;
        int partySize = 120;
        int listenWindowSeconds = 12;
        int dedupeSeconds = 3;
        int commandTimeoutSeconds = 10;
        boolean showClientConfirmation = true;
        String workingDirectory = "";
        List<String> calibrationCommand = List.of();
        List<String> messagePatterns = List.of(
            "\\bvote\\s*party\\b.*?(\\d{1,3})\\s*/\\s*120\\b",
            "\\bvoteparty\\b.*?(\\d{1,3})\\s*/\\s*120\\b"
        );

        static Config load() {
            Path path = path();
            if (Files.exists(path)) {
                try {
                    Config loaded = GSON.fromJson(Files.readString(path), Config.class);
                    if (loaded != null) {
                        return loaded.withDefaults();
                    }
                } catch (IOException e) {
                    LOGGER.warn("Could not read VPTrack calibrator config; using defaults.", e);
                }
            }

            Config defaults = new Config();
            try {
                Files.createDirectories(path.getParent());
                Files.writeString(path, GSON.toJson(defaults) + "\n", StandardCharsets.UTF_8);
            } catch (IOException e) {
                LOGGER.warn("Could not write VPTrack calibrator default config.", e);
            }
            return defaults;
        }

        static Path path() {
            return FabricLoader.getInstance().getConfigDir().resolve(CONFIG_FILE);
        }

        Config withDefaults() {
            if (partySize <= 0) {
                partySize = 120;
            }
            if (listenWindowSeconds <= 0) {
                listenWindowSeconds = 12;
            }
            if (dedupeSeconds < 0) {
                dedupeSeconds = 0;
            }
            if (commandTimeoutSeconds <= 0) {
                commandTimeoutSeconds = 10;
            }
            if (calibrationCommand == null) {
                calibrationCommand = List.of();
            }
            if (messagePatterns == null || messagePatterns.isEmpty()) {
                messagePatterns = new Config().messagePatterns;
            }
            if (workingDirectory == null) {
                workingDirectory = "";
            }
            return this;
        }
    }
}
