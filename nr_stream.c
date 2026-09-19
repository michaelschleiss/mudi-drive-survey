/* Direct libdiag callback transport, avoiding diag_mdlog's disk buffers.
 * Build: zig cc -target aarch64-linux-musl -dynamic -O2 -pthread nr_stream.c
 *        -ldl -o nr_stream_aarch64
 * Protocol: 8 hex length digits, newline, payload; zero length is a heartbeat.
 * Uses the router's existing libdiag; no firmware or persistent router changes.
 */
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopping;
static pthread_mutex_t output_lock = PTHREAD_MUTEX_INITIALIZER;
static int output_fd;
static void halt(int sig) { (void)sig; stopping = 1; }
static int write_all(const void *data, size_t size) {
    const unsigned char *p = data;
    while (size) {
        ssize_t n = write(output_fd, p, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { stopping = 1; return -1; }
        p += n; size -= n;
    }
    return 0;
}
static int emit(unsigned char *data, int length, void *context) {
    (void)context;
    if (length < 0 || length > 16*1024*1024) return -1;
    pthread_mutex_lock(&output_lock);
    int remaining = length, result = 0;
    do {
        int count = remaining > 65536 ? 65536 : remaining;
        char header[10];
        snprintf(header, sizeof(header), "%08x\n", count);
        if (write_all(header, 9) || (count && write_all(data, count))) { result = -1; break; }
        if (count) data += count;
        remaining -= count;
    } while (remaining);
    pthread_mutex_unlock(&output_lock);
    return result;
}
static int (*send_data)(unsigned char *, int);
static int send_file(const char *name) {
    FILE *f = fopen(name, "rb");
    if (!f) return -1;
    unsigned char packet[4096] = {0x20, 0, 0, 0};
    int length = 4, c, result = 0;
    while ((c = fgetc(f)) != EOF) {
        if (length == sizeof(packet)) { result = -1; break; }
        packet[length++] = c;
        if (c == 0x7e) { send_data(packet, length); length = 4; }
    }
    if (length != 4) result = -1;
    fclose(f);
    return result;
}
int main(int argc, char **argv) {
    if (argc != 4) return 2; /* enable mask, cleanup mask, stop file */
    output_fd = dup(STDOUT_FILENO);
    if (output_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 3;
    signal(SIGINT, halt); signal(SIGTERM, halt); signal(SIGHUP, halt);
    signal(SIGPIPE, SIG_IGN);
    void *lib = dlopen("libdiag.so", RTLD_NOW | RTLD_GLOBAL);
    if (!lib) { fprintf(stderr, "%s\n", dlerror()); return 4; }
    int (*init)(void *) = dlsym(lib, "Diag_LSM_Init");
    int (*deinit)(void) = dlsym(lib, "Diag_LSM_DeInit");
    void (*reg)(int (*)(unsigned char *, int, void *), void *) = dlsym(lib, "diag_register_callback");
    void (*mode)(int, char *) = dlsym(lib, "diag_switch_logging");
    send_data = dlsym(lib, "diag_send_data");
    if (!init || !deinit || !reg || !mode || !send_data) return 5;
    if (!init(NULL)) return 6;
    reg(emit, NULL);
    mode(6, NULL);
    if (send_file(argv[1])) stopping = 1;
    time_t start = time(NULL);
    while (!stopping && time(NULL)-start < 3600 && access(argv[3], F_OK)) {
        emit(NULL, 0, NULL);
        usleep(250000);
    }
    send_file(argv[2]);
    usleep(200000);
    deinit();
    close(output_fd);
    return 0;
}
