/* Port Manager for the headset: a controller-driven front end for the Port Manager service (manager.py), which
 * does the downloading and installing. It talks to the service on 127.0.0.1 with plain HTTP and reads simple
 * tab-separated replies.
 *
 * Controls: D-pad/left stick to choose, A install or update, Y choose a version, X go back to the previous
 * version, Start check for updates, B close (or leave the version list). Keyboard: arrows, Enter, V, P, R, Esc. */

#include <SDL.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

#include "third_party/font8x8_basic.h"

#define DEFAULT_PORT 8643
#define MAX_PORTS 32
#define MAX_RELEASES 40

typedef struct {
    char id[64], name[128], installed[64], latest[64], status[32], message[256];
    float progress;
    int can_rollback;
} Port;

typedef struct {
    char tag[64], name[128], date[16];
    int prerelease;
} Release;

static SDL_Renderer* ren;
static int W, H, S; /* screen size and text scale */
static int service_port = DEFAULT_PORT;

/* ---------------------------------------------------------------- text */

static void color(Uint32 rgb) {
    SDL_SetRenderDrawColor(ren, (rgb >> 16) & 0xff, (rgb >> 8) & 0xff, rgb & 0xff, 255);
}

static int text(const char* s, int x, int y, int scale, Uint32 rgb) {
    static SDL_Rect rects[4096];
    int n = 0, cx = x;
    color(rgb);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c >= 128) c = '?';
        for (int row = 0; row < 8; row++) {
            unsigned char bits = (unsigned char)font8x8_basic[c][row];
            for (int col = 0; col < 8; col++)
                if (bits & (1 << col)) {
                    rects[n++] = (SDL_Rect){ cx + col * scale, y + row * scale, scale, scale };
                    if (n == 4096) {
                        SDL_RenderFillRects(ren, rects, n);
                        n = 0;
                    }
                }
        }
        cx += 8 * scale;
    }
    if (n) SDL_RenderFillRects(ren, rects, n);
    return cx - x;
}

/* Draws s cut to max_px (ending in "..." when cut). */
static void text_fit(const char* s, int x, int y, int scale, int max_px, Uint32 rgb) {
    char buf[300];
    int max_chars = max_px / (8 * scale);
    snprintf(buf, sizeof buf, "%s", s);
    if ((int)strlen(buf) > max_chars && max_chars > 3) {
        buf[max_chars - 3] = '\0';
        strcat(buf, "...");
    }
    text(buf, x, y, scale, rgb);
}

static void fill(int x, int y, int w, int h, Uint32 rgb) {
    color(rgb);
    SDL_RenderFillRect(ren, &(SDL_Rect){ x, y, w, h });
}

/* ---------------------------------------------------------------- service */

static void read_port(void) {
    char path[512], buf[2048];
    snprintf(path, sizeof path, "%s/.config/port-manager/config.json", getenv("HOME") ? getenv("HOME") : "");
    FILE* f = fopen(path, "r");
    if (!f) return;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = '\0';
    const char* p = strstr(buf, "\"port\"");
    if (p && (p = strchr(p, ':'))) {
        int v = atoi(p + 1);
        if (v > 0 && v < 65536) service_port = v;
    }
}

/* Sends a request to the service; returns the body (caller frees) or NULL if it isn't reachable. */
static char* http(const char* method, const char* path) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return NULL;
    struct timeval tv = { .tv_sec = 5 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(service_port) };
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(s, (struct sockaddr*)&a, sizeof a) != 0) {
        close(s);
        return NULL;
    }
    char req[1024];
    int n = snprintf(req, sizeof req, "%s %s HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n", method, path);
    if (write(s, req, n) != n) {
        close(s);
        return NULL;
    }
    size_t cap = 65536, len = 0;
    char* buf = malloc(cap);
    ssize_t r;
    while ((r = read(s, buf + len, cap - len - 1)) > 0) {
        len += r;
        if (len + 1 >= cap) buf = realloc(buf, cap *= 2);
    }
    close(s);
    buf[len] = '\0';
    char* body = strstr(buf, "\r\n\r\n");
    if (!body) {
        free(buf);
        return NULL;
    }
    memmove(buf, body + 4, strlen(body + 4) + 1);
    return buf;
}

/* Splits one tab-separated line into fields (in place). */
static int split(char* line, char** f, int max) {
    int n = 0;
    f[n++] = line;
    for (char* p = line; *p && n < max; p++)
        if (*p == '\t') {
            *p = '\0';
            f[n++] = p + 1;
        }
    return n;
}

static void copy(char* dst, size_t sz, const char* src) {
    snprintf(dst, sz, "%s", strcmp(src, "-") == 0 ? "" : src);
}

static int load_ports(Port* ports) {
    char* body = http("GET", "/api/state?fmt=tsv");
    if (!body) return -1;
    int n = 0;
    for (char* line = strtok(body, "\n"); line && n < MAX_PORTS; line = strtok(NULL, "\n")) {
        char* f[8];
        if (split(line, f, 8) < 8) continue;
        Port* p = &ports[n++];
        copy(p->id, sizeof p->id, f[0]);
        copy(p->name, sizeof p->name, f[1]);
        copy(p->installed, sizeof p->installed, f[2]);
        copy(p->latest, sizeof p->latest, f[3]);
        copy(p->status, sizeof p->status, f[4]);
        p->progress = (float)atof(f[5]);
        copy(p->message, sizeof p->message, f[6]);
        p->can_rollback = atoi(f[7]);
    }
    free(body);
    return n;
}

static int load_releases(const char* id, Release* rels, char* err, size_t errsz) {
    char path[256];
    snprintf(path, sizeof path, "/api/releases?fmt=tsv&id=%s", id);
    char* body = http("GET", path);
    err[0] = '\0';
    if (!body) {
        snprintf(err, errsz, "Port Manager service isn't running.");
        return 0;
    }
    int n = 0;
    for (char* line = strtok(body, "\n"); line && n < MAX_RELEASES; line = strtok(NULL, "\n")) {
        char* f[4];
        int k = split(line, f, 4);
        if (k >= 2 && strcmp(f[0], "!") == 0) {
            snprintf(err, errsz, "%s", f[1]);
            break;
        }
        if (k < 4) continue;
        Release* r = &rels[n++];
        copy(r->tag, sizeof r->tag, f[0]);
        copy(r->name, sizeof r->name, f[1]);
        copy(r->date, sizeof r->date, f[2]);
        r->prerelease = atoi(f[3]);
    }
    free(body);
    return n;
}

static void action(const char* what, const char* id, const char* tag) {
    char path[256];
    if (tag)
        snprintf(path, sizeof path, "/api/%s?id=%s&tag=%s", what, id, tag);
    else
        snprintf(path, sizeof path, "/api/%s?id=%s", what, id);
    free(http("POST", path));
}

static void try_start_service(void) {
    pid_t pid = fork();
    if (pid == 0) {
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, 1);
            dup2(devnull, 2);
        }
        execlp("systemctl", "systemctl", "--user", "start", "frame-port-manager.service", (char*)NULL);
        _exit(127);
    }
    if (pid > 0) waitpid(pid, NULL, 0);
}

/* ---------------------------------------------------------------- drawing */

static int busy_status(const char* s) {
    return !strcmp(s, "downloading") || !strcmp(s, "installing");
}

static Uint32 status_color(const Port* p) {
    if (busy_status(p->status)) return 0x4fa3ff;
    if (!strcmp(p->status, "update available")) return 0xf0b429;
    if (!strcmp(p->status, "up to date")) return 0x4cc38a;
    if (!strcmp(p->status, "error")) return 0xf2555a;
    return 0x93a0ad;
}

static const char* status_label(const Port* p) {
    if (!strcmp(p->status, "downloading")) return "Downloading";
    if (!strcmp(p->status, "installing")) return "Installing";
    if (!strcmp(p->status, "update available")) return "Update available";
    if (!strcmp(p->status, "up to date")) return "Up to date";
    if (!strcmp(p->status, "error")) return "Problem";
    return "Not installed";
}

static void legend(const char* s) {
    int y = H - H / 20 - 8 * S;
    fill(0, y - 12 * S, W, H - y + 12 * S, 0x151a20);
    text(s, W / 20, y, S, 0x93a0ad);
}

static void draw_ports(Port* ports, int n, int sel, const char* banner) {
    int m = W / 20, y = m;
    color(0x0f1216);
    SDL_RenderClear(ren);
    text("Port Manager", m, y, S * 2, 0xe8edf2);
    y += 16 * S + 6 * S;
    if (banner && *banner) {
        text_fit(banner, m, y, S, W - 2 * m, 0xf0b429);
        y += 14 * S;
    }
    if (n <= 0) {
        text(n < 0 ? "Starting the Port Manager service..." : "No ports in the catalog.", m, y + 8 * S, S, 0x93a0ad);
    }
    int row_h = 50 * S;
    int max_rows = (H - y - 40 * S) / row_h;
    int first = sel >= max_rows ? sel - max_rows + 1 : 0;
    for (int i = first; i < n && i < first + max_rows; i++) {
        Port* p = &ports[i];
        int x = m, w = W - 2 * m;
        fill(x, y, w, row_h - 6 * S, i == sel ? 0x1f2a36 : 0x181d23);
        if (i == sel) fill(x, y, 3 * S, row_h - 6 * S, 0x4fa3ff);
        int tx = x + 10 * S, ty = y + 8 * S;
        const char* st = status_label(p);
        int sw = (int)strlen(st) * 8 * S;
        text_fit(p->name, tx, ty, S, w - 30 * S - sw, 0xe8edf2);
        text(st, x + w - 10 * S - sw, ty, S, status_color(p));
        ty += 13 * S;
        char line[200];
        snprintf(line, sizeof line, "Installed: %s   Latest: %s", p->installed[0] ? p->installed : "-",
                 p->latest[0] ? p->latest : "-");
        text_fit(line, tx, ty, S, w - 20 * S, 0x93a0ad);
        ty += 12 * S;
        if (busy_status(p->status)) {
            int bw = w - 20 * S;
            fill(tx, ty + 2 * S, bw, 4 * S, 0x2a313a);
            fill(tx, ty + 2 * S, (int)(bw * p->progress), 4 * S, 0x4fa3ff);
        } else if (p->message[0]) {
            text_fit(p->message, tx, ty, S, w - 20 * S, !strcmp(p->status, "error") ? 0xf2555a : 0x4cc38a);
        }
        y += row_h;
    }
    legend("A Install/Update   Y Versions   X Previous   Start Check   B Close");
    SDL_RenderPresent(ren);
}

static void draw_releases(const Port* p, Release* rels, int n, int sel, const char* err) {
    int m = W / 20, y = m;
    color(0x0f1216);
    SDL_RenderClear(ren);
    text_fit(p->name, m, y, S * 2, W - 2 * m, 0xe8edf2);
    y += 16 * S + 6 * S;
    text("Choose a version to install:", m, y, S, 0x93a0ad);
    y += 16 * S;
    if (err[0]) text_fit(err, m, y, S, W - 2 * m, 0xf2555a);
    int row_h = 16 * S;
    int max_rows = (H - y - 40 * S) / row_h;
    int first = sel >= max_rows ? sel - max_rows + 1 : 0;
    for (int i = first; i < n && i < first + max_rows; i++) {
        Release* r = &rels[i];
        if (i == sel) fill(m, y - 4 * S, W - 2 * m, row_h, 0x1f2a36);
        char line[200];
        snprintf(line, sizeof line, "%s  %s%s%s", r->tag, r->date, !strcmp(r->tag, p->latest) ? "  (latest)" : "",
                 !strcmp(r->tag, p->installed) ? "  (installed)" : "");
        text_fit(line, m + 10 * S, y, S, W - 2 * m - 20 * S, i == sel ? 0xe8edf2 : 0x93a0ad);
        y += row_h;
    }
    legend("A Install this version   B Back");
    SDL_RenderPresent(ren);
}

/* ---------------------------------------------------------------- main */

enum { NONE, UP, DOWN, OK, BACK, VERSIONS, PREVIOUS, REFRESH };

static int map_button(int b) {
    switch (b) {
    case SDL_CONTROLLER_BUTTON_DPAD_UP: return UP;
    case SDL_CONTROLLER_BUTTON_DPAD_DOWN: return DOWN;
    case SDL_CONTROLLER_BUTTON_A: return OK;
    case SDL_CONTROLLER_BUTTON_B: return BACK;
    case SDL_CONTROLLER_BUTTON_Y: return VERSIONS;
    case SDL_CONTROLLER_BUTTON_X: return PREVIOUS;
    case SDL_CONTROLLER_BUTTON_START: return REFRESH;
    default: return NONE;
    }
}

static int map_key(SDL_Keycode k) {
    switch (k) {
    case SDLK_UP: return UP;
    case SDLK_DOWN: return DOWN;
    case SDLK_RETURN: return OK;
    case SDLK_ESCAPE: case SDLK_BACKSPACE: return BACK;
    case SDLK_v: return VERSIONS;
    case SDLK_p: return PREVIOUS;
    case SDLK_r: return REFRESH;
    default: return NONE;
    }
}

int main(void) {
    /* PM_SCREENSHOT=<file.bmp> draws one frame of the list into memory, saves it and exits (testing without a
     * display); PM_SIZE=WxH sets its size, PM_SCREEN=versions shows the version list for the first port. */
    const char* shot = getenv("PM_SCREENSHOT");
    SDL_Surface* shot_surface = NULL;
    if (shot) {
        int sw = 1280, sh = 720;
        if (getenv("PM_SIZE")) sscanf(getenv("PM_SIZE"), "%dx%d", &sw, &sh);
        SDL_Init(0);
        shot_surface = SDL_CreateRGBSurfaceWithFormat(0, sw, sh, 32, SDL_PIXELFORMAT_ARGB8888);
        ren = SDL_CreateSoftwareRenderer(shot_surface);
    } else {
        if (SDL_Init(SDL_INIT_VIDEO | SDL_INIT_GAMECONTROLLER) != 0) {
            fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
            return 1;
        }
        Uint32 flags = getenv("PM_WINDOWED") ? 0 : SDL_WINDOW_FULLSCREEN_DESKTOP;
        SDL_Window* win = SDL_CreateWindow("Port Manager", SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, 1280, 720, flags);
        if (!win) return 1;
        ren = SDL_CreateRenderer(win, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC);
        if (!ren) ren = SDL_CreateRenderer(win, -1, SDL_RENDERER_SOFTWARE);
    }
    if (!ren) return 1;
    SDL_ShowCursor(SDL_DISABLE);
    read_port();

    Port ports[MAX_PORTS];
    Release rels[MAX_RELEASES];
    int nports = -1, sel = 0, nrels = 0, rsel = 0, in_versions = 0, tried_start = 0;
    char err[256] = "", banner[256] = "";
    Uint32 next_poll = 0, axis_ready = 0;

    for (;;) {
        int act = NONE;
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_QUIT) goto done;
            if (e.type == SDL_CONTROLLERDEVICEADDED) SDL_GameControllerOpen(e.cdevice.which);
            if (e.type == SDL_CONTROLLERBUTTONDOWN) act = map_button(e.cbutton.button);
            if (e.type == SDL_KEYDOWN) act = map_key(e.key.keysym.sym);
            if (e.type == SDL_CONTROLLERAXISMOTION && e.caxis.axis == SDL_CONTROLLER_AXIS_LEFTY &&
                SDL_GetTicks() >= axis_ready && abs(e.caxis.value) > 24000) {
                act = e.caxis.value < 0 ? UP : DOWN;
                axis_ready = SDL_GetTicks() + 250;
            }
        }

        if (SDL_GetTicks() >= next_poll) {
            nports = load_ports(ports);
            if (nports < 0 && !tried_start) {
                tried_start = 1;
                try_start_service();
            }
            int busy = 0;
            for (int i = 0; i < nports; i++) busy |= busy_status(ports[i].status);
            next_poll = SDL_GetTicks() + (busy ? 400 : 2000);
            if (sel >= nports) sel = nports > 0 ? nports - 1 : 0;
            snprintf(banner, sizeof banner, "%s",
                     nports < 0 && tried_start && SDL_GetTicks() > 8000
                         ? "The Port Manager service isn't running. Restart the Frame and try again."
                         : "");
        }

        if (in_versions) {
            if (act == UP && rsel > 0) rsel--;
            if (act == DOWN && rsel < nrels - 1) rsel++;
            if (act == BACK) in_versions = 0;
            if (act == OK && nrels > 0) {
                action("install", ports[sel].id, rels[rsel].tag);
                in_versions = 0;
                next_poll = 0;
            }
        } else {
            if (act == UP && sel > 0) sel--;
            if (act == DOWN && sel < nports - 1) sel++;
            if (act == BACK) goto done;
            if (nports > 0) {
                if (act == OK) action("install", ports[sel].id, NULL);
                if (act == PREVIOUS && ports[sel].can_rollback) action("rollback", ports[sel].id, NULL);
                if (act == REFRESH) free(http("POST", "/api/refresh"));
                if (act == VERSIONS) {
                    nrels = load_releases(ports[sel].id, rels, err, sizeof err);
                    rsel = 0;
                    for (int i = 0; i < nrels; i++)
                        if (!strcmp(rels[i].tag, ports[sel].installed)) rsel = i;
                    in_versions = 1;
                }
                if (act != NONE) next_poll = SDL_GetTicks() + 150;
            }
        }

        SDL_GetRendererOutputSize(ren, &W, &H);
        S = H >= 1000 ? 3 : 2;
        if (shot && getenv("PM_SCREEN") && !strcmp(getenv("PM_SCREEN"), "versions") && nports > 0 && !in_versions) {
            nrels = load_releases(ports[0].id, rels, err, sizeof err);
            in_versions = 1;
        }
        if (in_versions)
            draw_releases(&ports[sel], rels, nrels, rsel, err);
        else
            draw_ports(ports, nports, sel, banner);
        if (shot) {
            SDL_SaveBMP(shot_surface, shot);
            goto done;
        }
        SDL_Delay(16);
    }
done:
    SDL_Quit();
    return 0;
}
