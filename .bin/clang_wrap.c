#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

/* Append a quoted string to a buffer, MSVCRT-style command-line escaping. */
static void append_quoted(char **dst, size_t *cap, size_t *len, const char *s)
{
    int has_ws = (strpbrk(s, " \t") != NULL);
    int quoted = has_ws;
    const char *p;
    for (p = s; *p; p++)
        if (*p == '"')
            quoted = 1;
    size_t n = strlen(s);
    size_t need = n + (quoted ? 2 : 0);
    if (*len > 0)
        need += 1; /* space separator */
    if (*len + need + 1 > *cap) {
        *cap = (*len + need + 1) * 2 + 64;
        *dst = (char *)realloc(*dst, *cap);
    }
    char *out = *dst + *len;
    if (*len > 0)
        *out++ = ' ';
    if (quoted)
        *out++ = '"';
    for (p = s; *p; p++) {
        if (*p == '\\') {
            /* Count the run of backslashes. */
            const char *q = p;
            while (*q == '\\')
                q++;
            if (*q == '"') {
                size_t k = (size_t)(q - p);
                while (k--)
                    *out++ = '\\';
            }
        }
        if (*p == '"') {
            /* Escape the quote itself with one backslash. */
            *out++ = '\\';
        }
        *out++ = *p;
    }
    if (quoted)
        *out++ = '"';
    *len = (size_t)(out - *dst);
    *out = '\0';
}

int main(int argc, char **argv)
{
    const char *real_clang = "C:/Program Files/LLVM/bin/clang.exe";
    const char *target_arg = "--target=x86_64-w64-windows-gnu";
    const char *gcc_dir = "--gcc-install-dir=C:/ProgramData/mingw64/mingw64";
    /* The exe path has spaces; argv[0] here is the wrapper's own path. */

    size_t cap = 1024, len = 0;
    char *cmd = (char *)malloc(cap);
    if (!cmd)
        return 1;
    /* Always quote argv[0] (the program to run): points to real clang. */
    append_quoted(&cmd, &cap, &len, real_clang);

    int i;
    for (i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (strcmp(a, "-target") == 0 || strcmp(a, "--target") == 0) {
            i++; /* drop the triple that follows */
            continue;
        }
        if (strncmp(a, "--target=", 9) == 0 || strncmp(a, "-target=", 8) == 0 ||
            strncmp(a, "--gcc-install-dir=", 18) == 0 ||
            strncmp(a, "--gcc-toolchain=", 16) == 0 ||
            strncmp(a, "-mtriple=", 9) == 0)
            continue;
        append_quoted(&cmd, &cap, &len, a);
    }
    append_quoted(&cmd, &cap, &len, target_arg);
    append_quoted(&cmd, &cap, &len, gcc_dir);
    cmd[len] = '\0';

    if (getenv("CPYTE_WRAP_DEBUG")) {
        FILE *f = fopen("C:/Users/SONHA~1/AppData/Local/Temp/opencode/wrap_cmd.txt", "a");
        if (f) {
            fprintf(f, "CMDLINE: %s\n", cmd);
            fclose(f);
        }
    }

    STARTUPINFOA si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);

    BOOL ok = CreateProcessA(
        real_clang,          /* lpApplicationName */
        cmd,                 /* lpCommandLine */
        NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi);
    if (!ok)
        return 1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}