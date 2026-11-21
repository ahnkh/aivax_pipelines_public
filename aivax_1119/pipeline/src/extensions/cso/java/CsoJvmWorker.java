import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;

/** Pipeline-owned transport. The supplied CsoGradeHandler and Fasoo JARs stay unchanged. */
public class CsoJvmWorker {
    private static final int MAX_FRAME_BYTES = 8 * 1024 * 1024;

    public static void main(String[] args) throws Exception {
        PrintStream protocolOutput = System.out;
        System.setOut(System.err);
        // Load and initialize the existing handler before announcing readiness.
        runHandler(new String[0]);
        DataInputStream input = new DataInputStream(new BufferedInputStream(System.in));
        DataOutputStream output = new DataOutputStream(new BufferedOutputStream(protocolOutput));
        output.write("AIVAX_CSO_V1\n".getBytes(StandardCharsets.UTF_8));
        output.flush();
        while (true) {
            long requestId;
            try {
                requestId = input.readLong();
            } catch (EOFException e) {
                return;
            }
            int count = input.readInt();
            if (count < 0 || count > 65536) {
                throw new IOException("Invalid file count: " + count);
            }
            String[] paths = new String[count];
            long frameBytes = 12;
            for (int i = 0; i < count; i++) {
                int length = input.readInt();
                frameBytes += 4L + length;
                if (length < 0 || frameBytes > MAX_FRAME_BYTES) {
                    throw new IOException("Invalid request size");
                }
                byte[] encoded = input.readNBytes(length);
                if (encoded.length != length) {
                    throw new EOFException("Connection closed while reading file path");
                }
                paths[i] = new String(encoded, StandardCharsets.UTF_8);
            }
            byte[] result = runHandler(paths);
            if (result.length > MAX_FRAME_BYTES) {
                throw new IOException("Response size exceeds limit");
            }
            output.writeLong(requestId);
            output.writeInt(result.length);
            output.write(result);
            output.flush();
        }
    }

    private static byte[] runHandler(String[] paths) throws IOException {
        ByteArrayOutputStream captured = new ByteArrayOutputStream();
        try (PrintStream handlerOutput = new PrintStream(captured, true, StandardCharsets.UTF_8)) {
            System.setOut(handlerOutput);
            try {
                CsoGradeHandler.main(paths);
            } finally {
                System.setOut(System.err);
            }
        }
        // The unchanged handler prints its JSON array as its final stdout line.
        String text = captured.toString(StandardCharsets.UTF_8).stripTrailing();
        int lastNewline = text.lastIndexOf('\n');
        if (lastNewline >= 0) {
            System.err.print(text.substring(0, lastNewline + 1));
        }
        String result = text.substring(lastNewline + 1).strip();
        if (!result.startsWith("[") || !result.endsWith("]")) {
            throw new IOException("Handler did not return a JSON array");
        }
        return result.getBytes(StandardCharsets.UTF_8);
    }
}
