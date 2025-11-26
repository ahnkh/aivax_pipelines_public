
// ipc_benchmark.c
// Compile: gcc ipc_benchmark.c -o ipc_benchmark -lpthread

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/select.h>
#include <time.h>
#include <pthread.h>

#define SOCKET_PATH "/tmp/pipeline.sock"
#define BUFFER_SIZE 8192

// --- test params ---
#define NUM_THREADS 8
#define REQS_PER_THREAD 200
#define SELECT_USEC 1000 // 1ms

// --- shared stats ---
pthread_mutex_t stats_lock = PTHREAD_MUTEX_INITIALIZER;
long total_count = 0;
double total_time = 0.0;
double min_time = 1e9;
double max_time = 0.0;

double timespec_diff_sec(struct timespec a, struct timespec b) {
    return (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) / 1e9;
}

void update_stats(double elapsed) {
    pthread_mutex_lock(&stats_lock);
    total_count++;
    total_time += elapsed;
    if (elapsed < min_time) min_time = elapsed;
    if (elapsed > max_time) max_time = elapsed;
    pthread_mutex_unlock(&stats_lock);
}

const char *request_json =
    "{"
    "\"filter_list\":[\"input_filter\",\"secret_filter\"],"
    "\"prompt\":\"내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요\","    
        "\"id\":\"\","
        "\"email\":\"\","
        "\"client_host\":\"\","
        "\"session_id\":\"\""    
    "}";

void *thread_func(void *arg) {
    (void)arg;
    int sock;
    struct sockaddr_un addr;
    char buffer[BUFFER_SIZE];

    // create & connect socket
    sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock < 0) {
        perror("socket");
        return NULL;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path)-1);

    if (connect(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("connect");
        close(sock);
        return NULL;
    }

    // set non-blocking
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);

    for (int i = 0; i < REQS_PER_THREAD; ++i) {
        struct timespec t_start, t_end;
        clock_gettime(CLOCK_MONOTONIC, &t_start);

        // send request (no newline required unless server expects)
        ssize_t w = write(sock, request_json, strlen(request_json));
        if (w < 0) {
            perror("write");
            // continue but record an error-like long time
        }

        // wait for response with select (timeout small)
        fd_set readfds;
        struct timeval tv;
        FD_ZERO(&readfds);
        FD_SET(sock, &readfds);
        tv.tv_sec = 0;
        tv.tv_usec = SELECT_USEC; // microseconds

        int ret = select(sock+1, &readfds, NULL, NULL, &tv);
        int n = 0;
        if (ret > 0 && FD_ISSET(sock, &readfds)) {
            n = read(sock, buffer, sizeof(buffer)-1);
            if (n > 0) buffer[n] = '\0';
            else buffer[0] = '\0';
        } else {
            // timeout or error
            buffer[0] = '\0';
        }

        clock_gettime(CLOCK_MONOTONIC, &t_end);
        double elapsed = timespec_diff_sec(t_start, t_end);
        update_stats(elapsed);

        // Optionally print some samples
        if (i < 3) {
            printf("[IPC sample] thread %lu req %d elapsed=%.9f sec, resp_len=%d\n",
                   (unsigned long)pthread_self(), i+1, elapsed, n);
        }

        // small yield to avoid starving server (optional)
        // usleep(100); // uncomment if server is overwhelmed
    }

    close(sock);
    return NULL;
}

int main() {
    pthread_t threads[NUM_THREADS];

    printf("IPC benchmark: threads=%d, reqs_per_thread=%d\n", NUM_THREADS, REQS_PER_THREAD);

    for (int i = 0; i < NUM_THREADS; ++i) {
        if (pthread_create(&threads[i], NULL, thread_func, NULL) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    for (int i = 0; i < NUM_THREADS; ++i) {
        pthread_join(threads[i], NULL);
    }

    printf("\n=== IPC Results ===\n");
    printf("total requests: %ld\n", total_count);
    if (total_count > 0) {
        printf("avg: %.9f sec, min: %.9f sec, max: %.9f sec\n",
               total_time / total_count, min_time, max_time);
    }
    return 0;
}
