// fastapi_client.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <curl/curl.h>

double get_elapsed(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
}

size_t write_callback(void *ptr, size_t size, size_t nmemb, void *userdata) {
    strncat((char *)userdata, (char *)ptr, size*nmemb);
    return size*nmemb;
}

int main() {
    CURL *curl;
    CURLcode res;
    char response[4096];

    struct timespec start, end;

    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers, "Content-Type: application/json");


    curl_global_init(CURL_GLOBAL_DEFAULT);
    curl = curl_easy_init();

    if(curl) {
        const char *url = "http://127.0.0.1:7000/v1/filter/multiple_filter";
        // const char *json_data = "{\"router_point\":\"multiple_filter\",\"filter_list\":[\"input_filter\",\"secret_filter\"],\"prompt\":\"API key 예제\"}";

        const char *json_data = "{"
            "\"filter_list\":[\"input_filter\",\"secret_filter\"],"
            "\"prompt\":\"내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요\","
            "\"user_role\":{"
                "\"id\":\"\","
                "\"email\":\"\","
                "\"client_host\":\"\","
                "\"session_id\":\"\""
            "}"
        "}";

        for(int i=0; i<100; i++) {

            // printf("요청: %s\n", json_data);

            response[0] = '\0';
            clock_gettime(CLOCK_MONOTONIC, &start);
            
            curl_easy_setopt(curl, CURLOPT_URL, url);
            curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, json_data);
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)strlen(json_data));
            curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
            curl_easy_setopt(curl, CURLOPT_WRITEDATA, response);

            res = curl_easy_perform(curl);

            clock_gettime(CLOCK_MONOTONIC, &end);
            double elapsed = get_elapsed(start, end);

            // if(res != CURLE_OK)
            //     fprintf(stderr, "curl_easy_perform() failed: %s\n", curl_easy_strerror(res));
            // else
            //     printf("응답: %s\n", response);

            printf("소요 시간: %.6f초\n", elapsed);
        }

        curl_slist_free_all(headers);
        curl_easy_cleanup(curl);
    }

    curl_global_cleanup();
    return 0;
}