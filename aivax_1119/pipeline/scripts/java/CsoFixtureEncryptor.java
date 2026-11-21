import java.io.InputStream;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import org.apache.poi.poifs.crypt.Decryptor;
import org.apache.poi.poifs.crypt.EncryptionInfo;
import org.apache.poi.poifs.crypt.EncryptionMode;
import org.apache.poi.poifs.crypt.Encryptor;
import org.apache.poi.poifs.filesystem.POIFSFileSystem;

/** 암호화 문서 fixture 전용. 기존 POI 라이브러리로 실제 Office 암호화를 수행한다. */
public final class CsoFixtureEncryptor {
    public static void main(String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException(
                "Usage: CsoFixtureEncryptor <source> <destination> <password> <mode>");
        }
        byte[] original = Files.readAllBytes(Path.of(args[0]));
        Path destination = Path.of(args[1]);
        String password = args[2];
        if (password.isEmpty()) {
            throw new IllegalArgumentException("테스트 암호는 비어 있으면 안 됩니다.");
        }
        try (POIFSFileSystem filesystem = new POIFSFileSystem()) {
            EncryptionInfo info = new EncryptionInfo(EncryptionMode.valueOf(args[3]));
            Encryptor encryptor = info.getEncryptor();
            encryptor.confirmPassword(password);
            try (OutputStream encrypted = encryptor.getDataStream(filesystem)) {
                encrypted.write(original);
            }
            try (OutputStream output = Files.newOutputStream(destination)) {
                filesystem.writeFilesystem(output);
            }
        }
        // 비밀번호가 실제로 필요하고 올바른 암호로 원본을 복원할 수 있어야 한다.
        try (InputStream input = Files.newInputStream(destination);
             POIFSFileSystem filesystem = new POIFSFileSystem(input)) {
            Decryptor decryptor = new EncryptionInfo(filesystem).getDecryptor();
            if (decryptor.verifyPassword("") || decryptor.verifyPassword(password + "-wrong")) {
                throw new IllegalStateException("암호 없이 또는 잘못된 암호로 문서가 열립니다.");
            }
            if (!decryptor.verifyPassword(password)) {
                throw new IllegalStateException("올바른 암호로 문서를 열 수 없습니다.");
            }
            try (InputStream decrypted = decryptor.getDataStream(filesystem)) {
                if (!Arrays.equals(original, decrypted.readAllBytes())) {
                    throw new IllegalStateException("암호화 전후 문서 내용이 다릅니다.");
                }
            }
        }
    }
}
