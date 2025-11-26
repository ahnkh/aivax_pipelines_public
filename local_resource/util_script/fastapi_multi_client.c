
// http_benchmark.c
// Compile: gcc http_benchmark.c -o http_benchmark -lcurl -lpthread
// Requires libcurl dev package.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <pthread.h>
#include <curl/curl.h>

#define NUM_THREADS 8
#define REQS_PER_THREAD 200

// FastAPI URL (loopback)
#define URL "http://127.0.0.1:7000/v1/filter/multiple_filter"

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

// libcurl write callback (discard body, but capture if needed)
size_t write_cb(void *ptr, size_t size, size_t nmemb, void *userdata) {
    (void) userdata;
    return size * nmemb;
}

const char *json_payload =
    "{"
    "\"filter_list\":[\"input_filter\",\"secret_filter\"],"
    "\"prompt\":\"내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요\","    
    "\"user_id\":\"\","
    "\"email\":\"\","
    "\"client_host\":\"\","
    "\"session_id\":\"\""    
    "}";

void *thread_func(void *arg) {
    (void)arg;
    CURL *curl = curl_easy_init();
    struct curl_slist *headers = NULL;
    if (!curl) return NULL;

    headers = curl_slist_append(headers, "Content-Type: application/json");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);

    // Reuse curl handle for multiple requests
    for (int i = 0; i < REQS_PER_THREAD; ++i) {
        struct timespec t_start, t_end;
        clock_gettime(CLOCK_MONOTONIC, &t_start);

        curl_easy_setopt(curl, CURLOPT_URL, URL);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_payload);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)strlen(json_payload));

        CURLcode res = curl_easy_perform(curl);

        clock_gettime(CLOCK_MONOTONIC, &t_end);
        double elapsed = timespec_diff_sec(t_start, t_end);
        update_stats(elapsed);

        if (res != CURLE_OK) {
            fprintf(stderr, "curl error: %s\n", curl_easy_strerror(res));
        }

        if (i < 3) {
            printf("[HTTP sample] thread %lu req %d elapsed=%.9f sec\n",
                   (unsigned long)pthread_self(), i+1, elapsed);
        }
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return NULL;
}

int main() {
    pthread_t threads[NUM_THREADS];

    // init curl globally once
    curl_global_init(CURL_GLOBAL_ALL);

    printf("HTTP benchmark: threads=%d, reqs_per_thread=%d, url=%s\n", NUM_THREADS, REQS_PER_THREAD, URL);

    for (int i = 0; i < NUM_THREADS; ++i) {
        if (pthread_create(&threads[i], NULL, thread_func, NULL) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    for (int i = 0; i < NUM_THREADS; ++i) {
        pthread_join(threads[i], NULL);
    }

    curl_global_cleanup();

    printf("\n=== HTTP Results ===\n");
    printf("total requests: %ld\n", total_count);
    if (total_count > 0) {
        printf("avg: %.9f sec, min: %.9f sec, max: %.9f sec\n",
               total_time / total_count, min_time, max_time);
    }
    return 0;
}
