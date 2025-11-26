// ipc_client.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/select.h>
#include <time.h>

#define SOCKET_PATH "/tmp/pipeline.sock"
#define BUFFER_SIZE 4096

double get_elapsed(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
}

int main() {
    int sock;
    struct sockaddr_un addr;
    char buffer[BUFFER_SIZE];

    sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) {
        perror("socket");
        exit(1);
    }

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path)-1);

    if (connect(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("connect");
        exit(1);
    }

     // 논블로킹 모드 설정
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);

    const char *request = "{"
    "\"router.point\":\"multiple_filter\","
    "\"filter_list\":[\"input_filter\",\"secret_filter\"],"
    "\"prompt\":\"내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요\","
    "\"user_id\":\"\","
    "\"email\":\"\","
    "\"client_host\":\"\","
    "\"session_id\":\"\""
"}\n";

    
    // for (int i = 0; i < 2; i++) {
    //     struct timespec start, end;
    //     clock_gettime(CLOCK_MONOTONIC, &start);


    //     // printf("요청: %s\n", request);

    //     // send request
    //     write(sock, request, strlen(request));

    //     // recv response
    //     int n = read(sock, buffer, sizeof(buffer)-1);
    //     if (n > 0) {
    //         buffer[n] = '\0';
    //     } else {
    //         strcpy(buffer, "No response");
    //     }

    //     clock_gettime(CLOCK_MONOTONIC, &end);
    //     double elapsed = get_elapsed(start, end);

    //     // printf("응답: %s\n", buffer);
    //     printf("소요 시간: %.6f초\n", elapsed);
    // }

    struct timespec start, end;
    double total_elapsed = 0.0;

    for (int i = 0; i < 100; i++) 
    {
        clock_gettime(CLOCK_MONOTONIC, &start);

        // 요청 전송
        if (write(sock, request, strlen(request)) < 0) {
            perror("write");
            continue;
        }

        // 응답 받기
        buffer[0] = '\0';
        fd_set readfds;
        struct timeval tv;

        FD_ZERO(&readfds);
        FD_SET(sock, &readfds);
        tv.tv_sec = 0;
        tv.tv_usec = 1000;  // 1ms 대기

        int ret = select(sock+1, &readfds, NULL, NULL, &tv);
        if (ret > 0 && FD_ISSET(sock, &readfds)) {
            int n = read(sock, buffer, sizeof(buffer)-1);
            if (n > 0) buffer[n] = '\0';
            else strcpy(buffer, "No response");
        } else {
            strcpy(buffer, "No response (timeout)");
        }

        // printf("응답: %s\n", buffer);

        clock_gettime(CLOCK_MONOTONIC, &end);
        double elapsed = get_elapsed(start, end);
        total_elapsed += elapsed;

        // 결과 출력
        // if (i < 5) // 첫 5회만 출력
        // printf("[%d] 응답: %s, 소요 시간: %.6f초\n", i+1, buffer, elapsed);
        printf("[%d] 응답: 소요 시간: %.6f초\n", i+1, elapsed);

        usleep(10000); //0.001초
    }

    close(sock);
    return 0;
}