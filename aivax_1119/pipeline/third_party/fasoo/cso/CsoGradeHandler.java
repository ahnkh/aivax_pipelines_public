import com.fasoo.doc.DocProperties;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public class CsoGradeHandler {
    private static final String STATUS_TAGGED = "tagged"; // 등급 있음(1·2·3)
    private static final String STATUS_UNTAGGED = "untagged"; // FDRClass 값을 얻지 못함
    private static final String STATUS_ERROR = "error"; // 조회 중 예외 발생

    public static void main(String[] args) {
        DocProperties dp = new DocProperties();
        List<String> results = new ArrayList<>();

        for (String input : args) {
            String result;
            try {
                Path file = Path.of(input).toAbsolutePath().normalize();
                result = readGrade(dp, file);
            } catch (Exception e) {
                result = toJson(null, STATUS_ERROR);
                System.err.println("[Exception] " + input + " — " + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
            results.add(result);
        }

        // 입력 파일 순서대로 결과를 JSON 배열로 출력한다.
        System.out.println("[" + String.join(",", results) + "]");
    }

    private static String readGrade(DocProperties dp, Path file) throws Exception {
        if (!Files.isRegularFile(file)) {
            throw new IOException("File does not exist or is not a regular file: " + file);
        }

        String value = dp.getCTProperty(file.toString(), "FDRClass");
        // 미태깅과 미지원 형식 등을 구분하지 않고 등급 없음으로 처리한다.
        if (value == null || value.isBlank()) {
            return toJson(null, STATUS_UNTAGGED);
        }

        value = value.trim();
        if (!value.matches("[1-3]")) {
            throw new IllegalArgumentException("Invalid FDRClass value: " + value);
        }
        return toJson(Integer.valueOf(value), STATUS_TAGGED);
    }

    private static String toJson(Integer grade, String status) {
        return "{\"grade\":" + grade + ",\"status\":\"" + status + "\"}";
    }
}
