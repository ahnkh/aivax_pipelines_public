import com.fasoo.doc.DocProperties;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Objects;

/** 테스트 데이터 전용. 태깅과 재조회 모두 제공된 Fasoo 라이브러리에 위임한다. */
public final class CsoFixtureTagger {
    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("Usage: CsoFixtureTagger <file> <grade>");
        }
        Path file = Path.of(args[0]).toAbsolutePath().normalize();
        if (!Files.isRegularFile(file)) {
            throw new IllegalArgumentException("테스트 파일이 없습니다: " + file);
        }
        DocProperties properties = new DocProperties();
        // 파일 형식이나 XML을 직접 수정하지 않고 vendor의 태깅 API를 호출한다.
        if (!properties.setCTProperty(file.toString(), "FDRClass", args[1])) {
            throw new IllegalStateException("CSO 테스트 파일 태깅 실패: " + file);
        }
        // 태깅 실패를 미태깅 차단 성공으로 오인하지 않도록 즉시 재조회한다.
        String actual = properties.getCTProperty(file.toString(), "FDRClass");
        if (!Objects.equals(args[1], actual)) {
            throw new IllegalStateException(
                "CSO 태깅 재조회 불일치: expected=" + args[1] + ", actual=" + actual);
        }
    }
}
