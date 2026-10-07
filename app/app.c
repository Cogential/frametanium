/* Port Manager for the headset: a controller-driven front end for the Port Manager service (manager.py), which
 * does the downloading and installing. It talks to the service on 127.0.0.1 with plain HTTP and reads simple
 * tab-separated replies.
 *
 * Screens: the app list; an app's menu (install/update, versions, previous version, pre-releases, remove); adding
 * an app (type a GitHub repo on an on-screen keyboard, paste a link, or pick a suggestion); and the found repo,
 * where you pick its download and add it.
 *
 * Controls: D-pad/left stick to choose, A select, B back/close, Y add an app, Select update all, Start check for
 * updates. Keyboard: arrows, Enter, Esc, and typing on the on-screen keyboard screen. */

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
#define MAX_APPS 64
#define MAX_RELEASES 40
#define MAX_SUGGEST 32
#define MAX_ASSETS 16

typedef struct {
    char id[64], name[128], installed[64], latest[64], status[32], message[256], repo[128], mode[16], job[16];
    float progress;
    int can_rollback, prereleases, builtin;
} App;

typedef struct {
    char tag[64], name[128], date[16];
    int prerelease;
} Release;

typedef struct {
    char repo[128], name[128], note[160];
} Suggestion;

typedef struct {
    char repo[128], name[128], description[300], tag[64], date[16], existing[64];
    int prereleases, nassets, asset;
    char assets[MAX_ASSETS][128];
    int arm64[MAX_ASSETS];
    long sizes[MAX_ASSETS];
} Found;

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
        if (c >= 128) continue; /* the font is ASCII only: leave other characters out */
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
    char buf[400];
    int max_chars = max_px / (8 * scale);
    snprintf(buf, sizeof buf, "%s", s);
    if ((int)strlen(buf) > max_chars && max_chars > 3) {
        buf[max_chars - 3] = '\0';
        strcat(buf, "...");
    }
    text(buf, x, y, scale, rgb);
}

/* Draws s word-wrapped into lines of max_px, at most max_lines; returns the height used. */
static int text_wrap(const char* s, int x, int y, int scale, int max_px, int max_lines, Uint32 rgb) {
    int per = max_px / (8 * scale), lines = 0;
    const char* p = s;
    while (*p && lines < max_lines) {
        int len = (int)strlen(p);
        int take = len;
        if (len > per) {
            take = per;
            while (take > 0 && p[take] != ' ') take--;
            if (take == 0) take = per;
        }
        char buf[400];
        snprintf(buf, sizeof buf, "%.*s", take, p);
        if (lines == max_lines - 1 && len > take && take > 3) strcpy(buf + take - 3, "...");
        text(buf, x, y + lines * 12 * scale, scale, rgb);
        p += take;
        while (*p == ' ') p++;
        lines++;
    }
    return lines * 12 * scale;
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
static char* http(const char* method, const char* path, int timeout_s) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return NULL;
    struct timeval tv = { .tv_sec = timeout_s };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(service_port) };
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(s, (struct sockaddr*)&a, sizeof a) != 0) {
        close(s);
        return NULL;
    }
    char req[2048];
    int n = snprintf(req, sizeof req, "%s %s HTTP/1.0\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n", method, path);
    if (n >= (int)sizeof req || write(s, req, n) != n) {
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

/* Percent-encodes s for a query string. */
static void urlenc(char* out, size_t sz, const char* s) {
    static const char hex[] = "0123456789ABCDEF";
    size_t o = 0;
    for (; *s && o + 4 < sz; s++) {
        unsigned char c = (unsigned char)*s;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strchr("-_.~", c)) {
            out[o++] = c;
        } else {
            out[o++] = '%';
            out[o++] = hex[c >> 4];
            out[o++] = hex[c & 15];
        }
    }
    out[o] = '\0';
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

/* Lines of a reply, one at a time (strtok_r so callers can nest). */
#define EACH_LINE(body, line, save) for (char* line = strtok_r(body, "\n", &save); line; line = strtok_r(NULL, "\n", &save))

static int load_apps(App* apps, char* notice, size_t nsz, int* notice_busy) {
    char* body = http("GET", "/api/state?fmt=tsv", 5);
    if (!body) return -1;
    int n = 0;
    char* save;
    notice[0] = '\0';
    *notice_busy = 0;
    EACH_LINE(body, line, save) {
        char* f[13];
        int k = split(line, f, 13);
        if (k >= 3 && !strcmp(f[0], "#")) {
            copy(notice, nsz, f[2]);
            *notice_busy = !strcmp(f[1], "installing");
            continue;
        }
        if (k < 13 || n >= MAX_APPS) continue;
        App* p = &apps[n++];
        copy(p->id, sizeof p->id, f[0]);
        copy(p->name, sizeof p->name, f[1]);
        copy(p->installed, sizeof p->installed, f[2]);
        copy(p->latest, sizeof p->latest, f[3]);
        copy(p->status, sizeof p->status, f[4]);
        p->progress = (float)atof(f[5]);
        copy(p->message, sizeof p->message, f[6]);
        p->can_rollback = atoi(f[7]);
        copy(p->repo, sizeof p->repo, f[8]);
        copy(p->mode, sizeof p->mode, f[9]);
        p->prereleases = atoi(f[10]);
        p->builtin = atoi(f[11]);
        copy(p->job, sizeof p->job, f[12]);
    }
    free(body);
    return n;
}

static int load_releases(const char* id, Release* rels, char* err, size_t errsz) {
    char path[256];
    snprintf(path, sizeof path, "/api/releases?fmt=tsv&id=%s", id);
    char* body = http("GET", path, 25);
    err[0] = '\0';
    if (!body) {
        snprintf(err, errsz, "Port Manager service isn't running.");
        return 0;
    }
    int n = 0;
    char* save;
    EACH_LINE(body, line, save) {
        char* f[4];
        int k = split(line, f, 4);
        if (k >= 2 && strcmp(f[0], "!") == 0) {
            snprintf(err, errsz, "%s", f[1]);
            break;
        }
        if (k < 4 || n >= MAX_RELEASES) continue;
        Release* r = &rels[n++];
        copy(r->tag, sizeof r->tag, f[0]);
        copy(r->name, sizeof r->name, f[1]);
        copy(r->date, sizeof r->date, f[2]);
        r->prerelease = atoi(f[3]);
    }
    free(body);
    if (n == 0 && !err[0]) snprintf(err, errsz, "No versions with a download for the Frame.");
    return n;
}

static int load_suggestions(Suggestion* sug) {
    char* body = http("GET", "/api/suggestions?fmt=tsv", 5);
    if (!body) return 0;
    int n = 0;
    char* save;
    EACH_LINE(body, line, save) {
        char* f[4];
        if (split(line, f, 4) < 4 || n >= MAX_SUGGEST || atoi(f[3])) continue;
        Suggestion* s = &sug[n++];
        copy(s->repo, sizeof s->repo, f[0]);
        copy(s->name, sizeof s->name, f[1]);
        copy(s->note, sizeof s->note, f[2]);
    }
    free(body);
    return n;
}

/* Looks a repo up; fills fd and returns 1, or writes the problem to err and returns 0. */
static int inspect(const char* repo, Found* fd, char* err, size_t errsz) {
    char enc[600], path[700];
    urlenc(enc, sizeof enc, repo);
    snprintf(path, sizeof path, "/api/inspect?fmt=tsv&repo=%s", enc);
    char* body = http("GET", path, 30);
    memset(fd, 0, sizeof *fd);
    err[0] = '\0';
    if (!body) {
        snprintf(err, errsz, "The Port Manager service didn't answer.");
        return 0;
    }
    char* save;
    int ok = 0;
    EACH_LINE(body, line, save) {
        char* f[9];
        int k = split(line, f, 9);
        if (k >= 2 && !strcmp(f[0], "!")) {
            snprintf(err, errsz, "%s", f[1]);
            break;
        }
        if (k >= 8 && !strcmp(f[0], "=")) {
            copy(fd->repo, sizeof fd->repo, f[1]);
            copy(fd->name, sizeof fd->name, f[2]);
            copy(fd->description, sizeof fd->description, f[3]);
            copy(fd->tag, sizeof fd->tag, f[4]);
            copy(fd->date, sizeof fd->date, f[5]);
            fd->prereleases = atoi(f[6]);
            copy(fd->existing, sizeof fd->existing, f[7]);
            ok = 1;
        } else if (k >= 3 && ok && fd->nassets < MAX_ASSETS) {
            copy(fd->assets[fd->nassets], sizeof fd->assets[0], f[0]);
            fd->sizes[fd->nassets] = atol(f[1]);
            fd->arm64[fd->nassets] = atoi(f[2]);
            fd->nassets++;
        }
    }
    free(body);
    if (ok && fd->nassets == 0) {
        snprintf(err, errsz, "No downloads for the Frame in its latest release.");
        ok = 0;
    }
    return ok;
}

/* POSTs path; returns the reply's "!" error (or "") in err. */
static void post(const char* path, char* err, size_t errsz) {
    char* body = http("POST", path, 30);
    if (err) {
        err[0] = '\0';
        if (!body)
            snprintf(err, errsz, "The Port Manager service didn't answer.");
        else if (body[0] == '!' && body[1] == '\t')
            snprintf(err, errsz, "%.*s", (int)strcspn(body + 2, "\n"), body + 2);
        else if (strstr(body, "\"error\": \""))
            snprintf(err, errsz, "%.*s", (int)strcspn(strstr(body, "\"error\": \"") + 10, "\""),
                     strstr(body, "\"error\": \"") + 10);
    }
    free(body);
}

static void action(const char* what, const char* id, const char* extra) {
    char path[512];
    snprintf(path, sizeof path, "/api/%s?id=%s%s", what, id, extra ? extra : "");
    post(path, NULL, 0);
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

#define BG 0x0f1216
#define ROW 0x181d23
#define ROW_SEL 0x1f2a36
#define TXT 0xe8edf2
#define MUTED 0x93a0ad
#define ACCENT 0x4fa3ff
#define OK_C 0x4cc38a
#define WARN 0xf0b429
#define BAD 0xf2555a

static int busy_status(const char* s) {
    return !strcmp(s, "downloading") || !strcmp(s, "installing");
}

static Uint32 status_color(const App* p) {
    if (busy_status(p->status)) return ACCENT;
    if (!strcmp(p->status, "update available")) return WARN;
    if (!strcmp(p->status, "up to date")) return OK_C;
    if (!strcmp(p->status, "error")) return BAD;
    return MUTED;
}

static const char* status_label(const App* p) {
    if (!strcmp(p->status, "downloading")) return "Downloading";
    if (!strcmp(p->status, "installing")) return "Installing";
    if (!strcmp(p->status, "update available")) return "Update available";
    if (!strcmp(p->status, "up to date")) return "Up to date";
    if (!strcmp(p->status, "error")) return "Problem";
    return "Not installed";
}

static void clear(void) {
    color(BG);
    SDL_RenderClear(ren);
}

static int title(const char* s) {
    int m = W / 20;
    text_fit(s, m, m, S * 2, W - 2 * m, TXT);
    return m + 22 * S;
}

static void legend(const char* s) {
    int y = H - H / 20 - 8 * S;
    fill(0, y - 12 * S, W, H - y + 12 * S, 0x151a20);
    text_fit(s, W / 20, y, S, W - W / 10, MUTED);
}

/* First visible row so that sel stays on screen. */
static int scroll_first(int sel, int max_rows) {
    return sel >= max_rows ? sel - max_rows + 1 : 0;
}

static void draw_list(App* apps, int n, int sel, const char* banner, Uint32 banner_rgb) {
    clear();
    int m = W / 20, y = title("Port Manager");
    if (banner && *banner) {
        text_fit(banner, m, y, S, W - 2 * m, banner_rgb);
        y += 14 * S;
    }
    if (n < 0) {
        text("Starting the Port Manager service...", m, y + 8 * S, S, MUTED);
        legend("B Close");
        SDL_RenderPresent(ren);
        return;
    }
    int row_h = 50 * S;
    int max_rows = (H - y - 40 * S) / row_h;
    if (max_rows < 1) max_rows = 1;
    int first = scroll_first(sel, max_rows);
    for (int i = first; i <= n && i < first + max_rows; i++) {
        int x = m, w = W - 2 * m;
        if (i == n) { /* the "add" row */
            fill(x, y, w, 26 * S, i == sel ? ROW_SEL : ROW);
            if (i == sel) fill(x, y, 3 * S, 26 * S, ACCENT);
            text("+ Add an app from GitHub", x + 10 * S, y + 9 * S, S, ACCENT);
            break;
        }
        App* p = &apps[i];
        fill(x, y, w, row_h - 6 * S, i == sel ? ROW_SEL : ROW);
        if (i == sel) fill(x, y, 3 * S, row_h - 6 * S, ACCENT);
        int tx = x + 10 * S, ty = y + 8 * S;
        const char* st = status_label(p);
        int sw = (int)strlen(st) * 8 * S;
        text_fit(p->name, tx, ty, S, w - 30 * S - sw, TXT);
        text(st, x + w - 10 * S - sw, ty, S, status_color(p));
        ty += 13 * S;
        char line[300];
        snprintf(line, sizeof line, "%s   Installed: %s   Latest: %s", p->repo, p->installed[0] ? p->installed : "-",
                 p->latest[0] ? p->latest : "-");
        text_fit(line, tx, ty, S, w - 20 * S, MUTED);
        ty += 12 * S;
        if (busy_status(p->status)) {
            int bw = w - 20 * S;
            fill(tx, ty + 2 * S, bw, 4 * S, 0x2a313a);
            fill(tx, ty + 2 * S, (int)(bw * p->progress), 4 * S, ACCENT);
        } else if (p->message[0]) {
            text_fit(p->message, tx, ty, S, w - 20 * S, !strcmp(p->job, "error") ? BAD : OK_C);
        }
        y += row_h;
    }
    legend("A Open   Y Add app   Select Update all   Start Check   B Close");
    SDL_RenderPresent(ren);
}

/* A simple list of choices under a heading. */
static void draw_menu(const char* heading, const char* sub, const char** items, int n, int sel, const char* err,
                      const char* keys) {
    clear();
    int m = W / 20, y = title(heading);
    if (sub && *sub) {
        text_fit(sub, m, y, S, W - 2 * m, MUTED);
        y += 16 * S;
    }
    if (err && *err) {
        y += text_wrap(err, m, y, S, W - 2 * m, 3, BAD) + 4 * S;
    }
    int row_h = 18 * S;
    int max_rows = (H - y - 40 * S) / row_h;
    if (max_rows < 1) max_rows = 1;
    int first = scroll_first(sel, max_rows);
    for (int i = first; i < n && i < first + max_rows; i++) {
        if (i == sel) {
            fill(m, y - 5 * S, W - 2 * m, row_h - 2 * S, ROW_SEL);
            fill(m, y - 5 * S, 3 * S, row_h - 2 * S, ACCENT);
        }
        text_fit(items[i], m + 10 * S, y, S, W - 2 * m - 20 * S, i == sel ? TXT : MUTED);
        y += row_h;
    }
    legend(keys);
    SDL_RenderPresent(ren);
}

static void draw_wait(const char* heading, const char* what) {
    clear();
    int m = W / 20, y = title(heading);
    text_fit(what, m, y + 8 * S, S, W - 2 * m, MUTED);
    SDL_RenderPresent(ren);
}

/* ---- on-screen keyboard */

static const char* KB_ROWS[] = { "1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm_./" };
static const char* KB_KEYS[] = { "Delete", "Paste", "Clear", "Done" };
#define KB_SPECIAL_ROW 4

static void draw_keyboard(const char* entry, int row, int col, const char* err) {
    clear();
    int m = W / 20, y = title("Add an app from GitHub");
    text("Type the repo, like owner/name (a full github.com link works too):", m, y, S, MUTED);
    y += 16 * S;
    fill(m, y, W - 2 * m, 20 * S, ROW);
    char shown[300];
    snprintf(shown, sizeof shown, "%s_", entry);
    int maxc = (W - 2 * m - 20 * S) / (8 * S);
    int len = (int)strlen(shown);
    text(shown + (len > maxc ? len - maxc : 0), m + 10 * S, y + 6 * S, S, TXT);
    y += 30 * S;
    if (err && *err) {
        text_fit(err, m, y, S, W - 2 * m, BAD);
    }
    y += 14 * S;
    int key = 30 * S, gap = 4 * S;
    int kw = 10 * key + 9 * gap;
    int x0 = (W - kw) / 2;
    for (int r = 0; r < 4; r++) {
        for (int c = 0; c < 10; c++) {
            int x = x0 + c * (key + gap), on = r == row && c == col;
            fill(x, y, key, key, on ? ACCENT : ROW_SEL);
            char ch[2] = { KB_ROWS[r][c], 0 };
            text(ch, x + key / 2 - 4 * S, y + key / 2 - 4 * S, S, on ? 0xffffff : TXT);
        }
        y += key + gap;
    }
    int sw = (kw - 3 * gap) / 4;
    for (int c = 0; c < 4; c++) {
        int x = x0 + c * (sw + gap), on = row == KB_SPECIAL_ROW && c == col;
        fill(x, y, sw, key, on ? ACCENT : ROW_SEL);
        int tw = (int)strlen(KB_KEYS[c]) * 8 * S;
        text(KB_KEYS[c], x + (sw - tw) / 2, y + key / 2 - 4 * S, S, on ? 0xffffff : TXT);
    }
    legend("A Type   X Delete   Start Done   B Cancel");
    SDL_RenderPresent(ren);
}

static void draw_found(const Found* fd, const char* err) {
    clear();
    int m = W / 20, y = title(fd->name);
    text_fit(fd->repo, m, y, S, W - 2 * m, ACCENT);
    y += 14 * S;
    if (fd->description[0]) y += text_wrap(fd->description, m, y, S, W - 2 * m, 3, MUTED) + 6 * S;
    char line[400];
    snprintf(line, sizeof line, "Latest release: %s  (%s)%s", fd->tag, fd->date,
             fd->prereleases ? "  pre-release" : "");
    text_fit(line, m, y, S, W - 2 * m, TXT);
    y += 20 * S;
    if (fd->existing[0]) {
        text("It's already on your list.", m, y, S, OK_C);
        legend("A Show it   B Back");
        SDL_RenderPresent(ren);
        return;
    }
    text("Download:", m, y, S, MUTED);
    y += 14 * S;
    fill(m, y - 5 * S, W - 2 * m, 18 * S, ROW_SEL);
    snprintf(line, sizeof line, "%s %s  (%.1f MB)%s %s", fd->nassets > 1 ? "<" : " ", fd->assets[fd->asset],
             fd->sizes[fd->asset] / 1048576.0, fd->arm64[fd->asset] ? "" : "  not ARM64", fd->nassets > 1 ? ">" : "");
    text_fit(line, m + 10 * S, y, S, W - 2 * m - 20 * S, fd->arm64[fd->asset] ? TXT : WARN);
    y += 22 * S;
    if (fd->nassets > 1) {
        snprintf(line, sizeof line, "%d downloads to choose from; Left/Right to switch.", fd->nassets);
        text(line, m, y, S, MUTED);
        y += 14 * S;
    }
    text_wrap("Port Manager unpacks it and either adds it to your Steam library or, if it comes with an install.sh, "
              "runs that. Only add apps you trust.", m, y, S, W - 2 * m, 3, MUTED);
    y += 40 * S;
    if (err && *err) text_wrap(err, m, y, S, W - 2 * m, 3, BAD);
    legend(fd->nassets > 1 ? "A Add and install   Left/Right Download   B Back" : "A Add and install   B Back");
    SDL_RenderPresent(ren);
}

/* ---------------------------------------------------------------- main */

enum { NONE, UP, DOWN, LEFT, RIGHT, OK, BACK, ADD, UPDATE_ALL, REFRESH, DELETE };
enum { LIST, MENU, VERSIONS, CONFIRM, ADDING, KEYBOARD, FOUND };

static int map_button(int b) {
    switch (b) {
    case SDL_CONTROLLER_BUTTON_DPAD_UP: return UP;
    case SDL_CONTROLLER_BUTTON_DPAD_DOWN: return DOWN;
    case SDL_CONTROLLER_BUTTON_DPAD_LEFT: return LEFT;
    case SDL_CONTROLLER_BUTTON_DPAD_RIGHT: return RIGHT;
    case SDL_CONTROLLER_BUTTON_A: return OK;
    case SDL_CONTROLLER_BUTTON_B: return BACK;
    case SDL_CONTROLLER_BUTTON_Y: return ADD;
    case SDL_CONTROLLER_BUTTON_X: return DELETE;
    case SDL_CONTROLLER_BUTTON_BACK: return UPDATE_ALL;
    case SDL_CONTROLLER_BUTTON_START: return REFRESH;
    default: return NONE;
    }
}

static int map_key(SDL_Keycode k, int typing) {
    switch (k) {
    case SDLK_UP: return UP;
    case SDLK_DOWN: return DOWN;
    case SDLK_LEFT: return LEFT;
    case SDLK_RIGHT: return RIGHT;
    case SDLK_RETURN: return typing ? REFRESH : OK; /* Enter finishes typing */
    case SDLK_ESCAPE: return BACK;
    case SDLK_BACKSPACE: return typing ? DELETE : BACK;
    default: break;
    }
    if (typing) return NONE;
    switch (k) {
    case SDLK_a: return ADD;
    case SDLK_u: return UPDATE_ALL;
    case SDLK_r: return REFRESH;
    default: return NONE;
    }
}

int main(void) {
    /* PM_SCREENSHOT=<file.bmp> draws one frame into memory, saves it and exits (testing without a display);
     * PM_SIZE=WxH sets its size, PM_SCREEN=menu|versions|add|keyboard|found picks the screen (for the first app;
     * PM_REPO is the repo for "found", PM_TYPED the text for "keyboard"). */
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
    SDL_StopTextInput();
    read_port();

    static App apps[MAX_APPS];
    static Release rels[MAX_RELEASES];
    static Suggestion sug[MAX_SUGGEST];
    static Found fd;
    int napps = -1, sel = 0, nrels = 0, rsel = 0, msel = 0, asel = 0, nsug = 0, tried_start = 0;
    int screen = LIST, kb_row = 1, kb_col = 0, notice_busy = 0;
    char err[300] = "", banner[300] = "", notice[300] = "", entry[256] = "", want_id[64] = "";
    char hint_name[128] = "";
    Uint32 next_poll = 0, axis_ready = 0;
    const char* menu_items[8];
    int menu_acts[8], nmenu = 0;
    char menu_text[8][160];
    enum { M_INSTALL, M_VERSIONS, M_PREVIOUS, M_PRE, M_REMOVE };

    if (shot && getenv("PM_SCREEN")) {
        const char* sc = getenv("PM_SCREEN");
        napps = load_apps(apps, notice, sizeof notice, &notice_busy);
        if (!strcmp(sc, "menu")) screen = MENU;
        if (!strcmp(sc, "versions") && napps > 0) {
            nrels = load_releases(apps[0].id, rels, err, sizeof err);
            screen = VERSIONS;
        }
        if (!strcmp(sc, "add")) {
            nsug = load_suggestions(sug);
            screen = ADDING;
        }
        if (!strcmp(sc, "keyboard")) {
            snprintf(entry, sizeof entry, "%s", getenv("PM_TYPED") ? getenv("PM_TYPED") : "");
            kb_row = 2, kb_col = 3;
            screen = KEYBOARD;
        }
        if (!strcmp(sc, "found")) {
            inspect(getenv("PM_REPO") ? getenv("PM_REPO") : "", &fd, err, sizeof err);
            screen = FOUND;
        }
        if (!strcmp(sc, "confirm")) screen = CONFIRM;
        next_poll = 0xffffffff;
    }

    for (;;) {
        int act = NONE;
        const char* typed = NULL;
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_QUIT) goto done;
            if (e.type == SDL_CONTROLLERDEVICEADDED) SDL_GameControllerOpen(e.cdevice.which);
            if (e.type == SDL_CONTROLLERBUTTONDOWN) act = map_button(e.cbutton.button);
            if (e.type == SDL_KEYDOWN) act = map_key(e.key.keysym.sym, screen == KEYBOARD);
            if (e.type == SDL_TEXTINPUT && screen == KEYBOARD) typed = e.text.text;
            if (e.type == SDL_CONTROLLERAXISMOTION && SDL_GetTicks() >= axis_ready && abs(e.caxis.value) > 24000) {
                if (e.caxis.axis == SDL_CONTROLLER_AXIS_LEFTY) act = e.caxis.value < 0 ? UP : DOWN;
                if (e.caxis.axis == SDL_CONTROLLER_AXIS_LEFTX) act = e.caxis.value < 0 ? LEFT : RIGHT;
                if (act != NONE) axis_ready = SDL_GetTicks() + 220;
            }
        }

        if (SDL_GetTicks() >= next_poll) {
            napps = load_apps(apps, notice, sizeof notice, &notice_busy);
            if (napps < 0 && !tried_start) {
                tried_start = 1;
                try_start_service();
            }
            int busy = notice_busy;
            for (int i = 0; i < napps; i++) busy |= busy_status(apps[i].status);
            next_poll = SDL_GetTicks() + (busy ? 400 : 2000);
            if (want_id[0]) /* jump to an app that was just added */
                for (int i = 0; i < napps; i++)
                    if (!strcmp(apps[i].id, want_id)) {
                        sel = i;
                        want_id[0] = '\0';
                    }
            if (sel > napps) sel = napps > 0 ? napps : 0;
            snprintf(banner, sizeof banner, "%s",
                     napps < 0 && tried_start && SDL_GetTicks() > 8000
                         ? "The Port Manager service isn't running. Restart the Frame and try again."
                         : notice);
        }
        App* cur = sel < napps ? &apps[sel] : NULL;

        switch (screen) {
        case LIST:
            if (act == UP && sel > 0) sel--;
            if (act == DOWN && sel < napps) sel++;
            if (act == BACK) goto done;
            if (napps < 0) break;
            if (act == OK && cur) {
                screen = MENU;
                msel = 0;
                err[0] = '\0';
            } else if (act == ADD || (act == OK && !cur)) {
                draw_wait("Add an app from GitHub", "Loading suggestions...");
                nsug = load_suggestions(sug);
                asel = 0;
                err[0] = '\0';
                screen = ADDING;
            }
            if (act == REFRESH) post("/api/refresh", NULL, 0);
            if (act == UPDATE_ALL) post("/api/update_all", NULL, 0);
            if (act != NONE) next_poll = SDL_GetTicks() + 150;
            break;

        case MENU:
            if (!cur) {
                screen = LIST;
                break;
            }
            nmenu = 0;
            if (cur->latest[0]) {
                if (!cur->installed[0])
                    snprintf(menu_text[nmenu], 160, "Install %s", cur->latest);
                else if (!strcmp(cur->status, "update available"))
                    snprintf(menu_text[nmenu], 160, "Update to %s", cur->latest);
                else
                    snprintf(menu_text[nmenu], 160, "Reinstall %s", cur->latest);
                menu_acts[nmenu++] = M_INSTALL;
            }
            snprintf(menu_text[nmenu], 160, "Choose a version...");
            menu_acts[nmenu++] = M_VERSIONS;
            if (cur->can_rollback) {
                snprintf(menu_text[nmenu], 160, "Go back to the previous version");
                menu_acts[nmenu++] = M_PREVIOUS;
            }
            snprintf(menu_text[nmenu], 160, "Pre-releases: %s", cur->prereleases ? "included" : "skipped");
            menu_acts[nmenu++] = M_PRE;
            snprintf(menu_text[nmenu], 160, "Remove...");
            menu_acts[nmenu++] = M_REMOVE;
            for (int i = 0; i < nmenu; i++) menu_items[i] = menu_text[i];
            if (msel >= nmenu) msel = nmenu - 1;
            if (act == UP && msel > 0) msel--;
            if (act == DOWN && msel < nmenu - 1) msel++;
            if (act == BACK) screen = LIST;
            if (act == OK) {
                int m = menu_acts[msel];
                if (m == M_INSTALL) {
                    action("install", cur->id, NULL);
                    screen = LIST;
                } else if (m == M_VERSIONS) {
                    draw_wait(cur->name, "Loading versions...");
                    nrels = load_releases(cur->id, rels, err, sizeof err);
                    rsel = 0;
                    for (int i = 0; i < nrels; i++)
                        if (!strcmp(rels[i].tag, cur->installed)) rsel = i;
                    screen = VERSIONS;
                } else if (m == M_PREVIOUS) {
                    action("rollback", cur->id, NULL);
                    screen = LIST;
                } else if (m == M_PRE) {
                    action("settings", cur->id, cur->prereleases ? "&prereleases=0" : "&prereleases=1");
                    cur->prereleases = !cur->prereleases;
                } else if (m == M_REMOVE) {
                    screen = CONFIRM;
                }
                next_poll = SDL_GetTicks() + 150;
            }
            break;

        case VERSIONS:
            if (act == UP && rsel > 0) rsel--;
            if (act == DOWN && rsel < nrels - 1) rsel++;
            if (act == BACK) screen = MENU;
            if (act == OK && nrels > 0 && cur) {
                char extra[100], enc[80];
                urlenc(enc, sizeof enc, rels[rsel].tag);
                snprintf(extra, sizeof extra, "&tag=%s", enc);
                action("install", cur->id, extra);
                screen = LIST;
                next_poll = 0;
            }
            break;

        case CONFIRM:
            if (act == BACK) screen = MENU;
            if (act == OK && cur) {
                action("remove", cur->id, NULL);
                screen = LIST;
                next_poll = SDL_GetTicks() + 300;
            }
            break;

        case ADDING: {
            int has_clip = SDL_HasClipboardText();
            int nitems = 1 + has_clip + nsug;
            if (act == UP && asel > 0) asel--;
            if (act == DOWN && asel < nitems - 1) asel++;
            if (act == BACK) screen = LIST;
            if (act == OK) {
                const char* repo = NULL;
                hint_name[0] = '\0';
                if (asel == 0) {
                    screen = KEYBOARD;
                    err[0] = '\0';
                } else if (has_clip && asel == 1) {
                    char* clip = SDL_GetClipboardText();
                    snprintf(entry, sizeof entry, "%s", clip ? clip : "");
                    SDL_free(clip);
                    repo = entry;
                } else {
                    Suggestion* s = &sug[asel - 1 - has_clip];
                    repo = s->repo;
                    snprintf(hint_name, sizeof hint_name, "%s", s->name);
                }
                if (repo) {
                    draw_wait("Add an app from GitHub", "Looking it up on GitHub...");
                    if (inspect(repo, &fd, err, sizeof err)) {
                        if (hint_name[0]) snprintf(fd.name, sizeof fd.name, "%s", hint_name);
                        err[0] = '\0';
                        screen = FOUND;
                    }
                }
            }
            if (screen == KEYBOARD) SDL_StartTextInput();
            break;
        }

        case KEYBOARD: {
            size_t len = strlen(entry);
            if (typed && len + strlen(typed) < sizeof entry - 1) strcat(entry, typed);
            if (act == UP) kb_row = kb_row > 0 ? kb_row - 1 : KB_SPECIAL_ROW;
            if (act == DOWN) kb_row = kb_row < KB_SPECIAL_ROW ? kb_row + 1 : 0;
            int cols = kb_row == KB_SPECIAL_ROW ? 4 : 10;
            if (kb_col >= cols) kb_col = cols - 1;
            if (act == LEFT) kb_col = (kb_col + cols - 1) % cols;
            if (act == RIGHT) kb_col = (kb_col + 1) % cols;
            int done_typing = act == REFRESH;
            if (act == DELETE && len) entry[len - 1] = '\0';
            if (act == OK) {
                if (kb_row < KB_SPECIAL_ROW) {
                    if (len < sizeof entry - 2) {
                        entry[len] = KB_ROWS[kb_row][kb_col];
                        entry[len + 1] = '\0';
                    }
                } else if (kb_col == 0 && len) {
                    entry[len - 1] = '\0';
                } else if (kb_col == 1) {
                    char* clip = SDL_GetClipboardText();
                    if (clip && len + strlen(clip) < sizeof entry - 1) strcat(entry, clip);
                    SDL_free(clip);
                } else if (kb_col == 2) {
                    entry[0] = '\0';
                } else if (kb_col == 3) {
                    done_typing = 1;
                }
            }
            if (act == BACK) {
                SDL_StopTextInput();
                screen = ADDING;
            }
            if (done_typing && entry[0]) {
                draw_wait("Add an app from GitHub", "Looking it up on GitHub...");
                if (inspect(entry, &fd, err, sizeof err)) {
                    err[0] = '\0';
                    SDL_StopTextInput();
                    screen = FOUND;
                }
            }
            break;
        }

        case FOUND:
            if (act == BACK) screen = ADDING;
            if (fd.existing[0]) {
                if (act == OK) {
                    snprintf(want_id, sizeof want_id, "%s", fd.existing);
                    screen = LIST;
                    next_poll = 0;
                }
                break;
            }
            if (act == LEFT && fd.nassets > 1) fd.asset = (fd.asset + fd.nassets - 1) % fd.nassets;
            if (act == RIGHT && fd.nassets > 1) fd.asset = (fd.asset + 1) % fd.nassets;
            if (act == OK) {
                char r[300], a[300], n[300], path[1100];
                urlenc(r, sizeof r, fd.repo);
                urlenc(a, sizeof a, fd.assets[fd.asset]);
                urlenc(n, sizeof n, fd.name);
                snprintf(path, sizeof path, "/api/add?fmt=tsv&install=1&repo=%s&asset=%s&name=%s", r, a, n);
                draw_wait(fd.name, "Adding it...");
                char* body = http("POST", path, 30);
                if (!body) {
                    snprintf(err, sizeof err, "The Port Manager service didn't answer.");
                } else if (body[0] == '!') {
                    snprintf(err, sizeof err, "%.*s", (int)strcspn(body + 2, "\n"), body + 2);
                } else {
                    if (body[0] == '=') snprintf(want_id, sizeof want_id, "%.*s", (int)strcspn(body + 2, "\n"), body + 2);
                    screen = LIST;
                    entry[0] = '\0';
                    next_poll = 0;
                }
                free(body);
            }
            break;
        }

        SDL_GetRendererOutputSize(ren, &W, &H);
        S = H >= 1000 ? 3 : 2;
        cur = sel < napps ? &apps[sel] : NULL;
        if (screen == MENU && cur) {
            char sub[300];
            snprintf(sub, sizeof sub, "%s   Installed: %s   Latest: %s", cur->repo,
                     cur->installed[0] ? cur->installed : "-", cur->latest[0] ? cur->latest : "-");
            draw_menu(cur->name, sub, menu_items, nmenu, msel, cur->message[0] && !strcmp(cur->job, "error") ? cur->message : "",
                      "A Select   B Back");
        } else if (screen == VERSIONS && cur) {
            const char* items[MAX_RELEASES];
            static char lines[MAX_RELEASES][200];
            for (int i = 0; i < nrels; i++) {
                snprintf(lines[i], sizeof lines[i], "%s  %s%s%s%s", rels[i].tag, rels[i].date,
                         rels[i].prerelease ? "  pre-release" : "", !strcmp(rels[i].tag, cur->latest) ? "  (latest)" : "",
                         !strcmp(rels[i].tag, cur->installed) ? "  (installed)" : "");
                items[i] = lines[i];
            }
            draw_menu(cur->name, "Choose a version to install:", items, nrels, rsel, err, "A Install this version   B Back");
        } else if (screen == CONFIRM && cur) {
            const char* items[] = { "Yes, remove it" };
            char sub[300];
            snprintf(sub, sizeof sub, "Remove %s?", cur->name);
            draw_menu(cur->name, sub, items, 1, 0,
                      cur->installed[0] ? "Its files are deleted. Saves and settings kept outside its folder stay." : "",
                      "A Remove   B Cancel");
        } else if (screen == ADDING) {
            const char* items[2 + MAX_SUGGEST];
            static char lines[MAX_SUGGEST][300];
            int k = 0, has_clip = SDL_HasClipboardText();
            items[k++] = "Type a GitHub repo...";
            if (has_clip) items[k++] = "Paste a link from the clipboard";
            for (int i = 0; i < nsug; i++) {
                snprintf(lines[i], sizeof lines[i], "%s  -  %s", sug[i].name, sug[i].note);
                items[k++] = lines[i];
            }
            draw_menu("Add an app from GitHub", nsug ? "Type a repo, or pick one made for the Frame:" : "", items, k, asel,
                      err, "A Select   B Back");
        } else if (screen == KEYBOARD) {
            draw_keyboard(entry, kb_row, kb_col, err);
        } else if (screen == FOUND) {
            draw_found(&fd, err);
        } else {
            if (screen != LIST) screen = LIST;
            int is_err = strstr(banner, "isn't running") != NULL;
            draw_list(apps, napps, sel, banner, is_err ? WARN : OK_C);
        }
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
