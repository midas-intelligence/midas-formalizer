/* Production import worker for the static Lean 4.35.0-rc3 runtime on Linux x86-64.
 * The import parent stays inside the original Bubblewrap sandbox. Landlock
 * confines request files and signals; ptrace admits syscalls and follows every
 * descendant until all have exited. libuv starts only in the request child.
 * Checker-owned build recipes link this with executor.imports.LINK_FLAGS.
 */
#define _GNU_SOURCE
#include <lean/lean.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sched.h>
#include <signal.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <dirent.h>
#include <fcntl.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/user.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/landlock.h>
#include <linux/audit.h>

static int request_child;
static int control_fd = -1;
extern void __real_initialize_libuv(void);
extern void __real__ZN4lean15event_loop_lockEPNS_12event_loop_tE(void *);
void __wrap_initialize_libuv(void) { /* Deliberately deferred until child setup. */ }
void __wrap__ZN4lean15event_loop_lockEPNS_12event_loop_tE(void *loop) {
    if (!request_child) {
        fputs("MIDAS_UNSUPPORTED import initializer needs libuv\n", stderr);
        _exit(125);
    }
    __real__ZN4lean15event_loop_lockEPNS_12event_loop_tE(loop);
}

static void die(const char *where) { perror(where); _exit(125); }
static unsigned thread_count(void) {
    unsigned n = 0;
    DIR *d = opendir("/proc/self/task");
    if (!d) return 0;
    struct dirent *e;
    while ((e = readdir(d))) if (e->d_name[0] >= '0' && e->d_name[0] <= '9') n++;
    closedir(d); return n;
}
static lean_obj_res error(const char *where) {
    char msg[512]; snprintf(msg, sizeof(msg), "%s: %s", where, strerror(errno));
    return lean_io_result_mk_error(lean_mk_io_user_error(lean_mk_string(msg)));
}
static int empty_directory(const char *path) {
    DIR *d = opendir(path);
    if (!d) return 0;
    struct dirent *e; int empty = 1;
    while ((e = readdir(d))) if (strcmp(e->d_name,".") && strcmp(e->d_name,"..")) empty = 0;
    closedir(d); return empty;
}
static int descriptors_clean(void) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) return 0;
    struct dirent *e; int clean = 1;
    while ((e = readdir(d))) {
        const char *end = e->d_name; long fd = 0;
        while (*end >= '0' && *end <= '9') fd = fd * 10 + (*end++ - '0');
        if (*end || fd <= 2 || fd == dirfd(d) || fd == control_fd) continue;
        fprintf(stderr, "MIDAS_UNSUPPORTED inherited descriptor %ld\n", fd);
        clean = 0;
    }
    closedir(d); return clean;
}

LEAN_EXPORT lean_obj_res midas_control_open(lean_obj_arg world) {
    (void)world;
    control_fd = open("/midas-inputs/.midas-control", O_WRONLY | O_CLOEXEC);
    if (control_fd < 0) return error("control open");
    return lean_io_result_mk_ok(lean_box(0));
}
LEAN_EXPORT lean_obj_res midas_control_write(b_lean_obj_arg line, lean_obj_arg world) {
    (void)world;
    const char *p = lean_string_cstr(line); size_t n = strlen(p);
    while (n) {
        ssize_t sent = write(control_fd, p, n);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) return error("control write");
        p += sent; n -= sent;
    }
    return lean_io_result_mk_ok(lean_box(0));
}

static void landlock_path(int rules, const char *path, uint64_t access) {
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0) die(path);
    struct landlock_path_beneath_attr rule = {.allowed_access = access, .parent_fd = fd};
    if (syscall(SYS_landlock_add_rule, rules, LANDLOCK_RULE_PATH_BENEATH, &rule, 0) < 0)
        die("landlock rule");
    close(fd);
}

static void restrict_request(void) {
    /* ABI 6 adds signal and abstract-UNIX-socket scopes. Do not silently weaken. */
    int abi = syscall(SYS_landlock_create_ruleset, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 6) { errno = ENOTSUP; die("landlock ABI 6 required"); }
    const uint64_t read = LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR;
    const uint64_t ordinary = ((1ULL << 15) - 1) &
        ~(LANDLOCK_ACCESS_FS_MAKE_CHAR | LANDLOCK_ACCESS_FS_MAKE_BLOCK);
    struct { uint64_t fs, net, scoped; } attr = {
        .fs = (1ULL << 16) - 1, /* Includes ABI 5 IOCTL_DEV. */
        .net = 3, .scoped = 3
    };
    int rules = syscall(SYS_landlock_create_ruleset, &attr, sizeof(attr), 0);
    if (rules < 0) die("landlock create");
    DIR *root = opendir("/");
    if (!root) die("root inventory");
    struct dirent *entry;
    while ((entry = readdir(root))) {
        const char *name = entry->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..") || !strcmp(name, "proc") ||
            !strcmp(name, "dev") || !strcmp(name, "sys") || !strcmp(name, "tmp") ||
            !strcmp(name, "midas-inputs")) continue;
        struct stat st;
        if (fstatat(dirfd(root), name, &st, AT_SYMLINK_NOFOLLOW) < 0) die("root stat");
        if (!S_ISDIR(st.st_mode)) continue;
        char path[512]; snprintf(path, sizeof(path), "/%s", name);
        landlock_path(rules, path, read | LANDLOCK_ACCESS_FS_EXECUTE);
    }
    closedir(root);
    landlock_path(rules, "/tmp", ordinary);
    landlock_path(rules, "/midas-inputs", read);
    landlock_path(rules, "/midas-inputs/.midas-evidence", LANDLOCK_ACCESS_FS_WRITE_FILE);
    landlock_path(rules, "/dev/null", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE);
    landlock_path(rules, "/dev/zero", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE);
    landlock_path(rules, "/dev/urandom", LANDLOCK_ACCESS_FS_READ_FILE);
    landlock_path(rules, "/dev/random", LANDLOCK_ACCESS_FS_READ_FILE);
    /* Only this process's proc files, never the parent's descriptors or memory.
     * mem is deliberately excluded; task children can use inherited proc fds. */
    const char *proc_files[] = {"/proc/self/status", "/proc/self/maps", "/proc/self/stat",
                              "/proc/self/statm", "/proc/cpuinfo", "/proc/meminfo",
                              "/proc/version_signature", "/proc/stat"};
    for (size_t i = 0; i < sizeof(proc_files)/sizeof(*proc_files); i++)
        landlock_path(rules, proc_files[i], LANDLOCK_ACCESS_FS_READ_FILE);
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0 ||
        syscall(SYS_landlock_restrict_self, rules, 0) < 0) die("landlock enforce");
    close(rules);
}

static int private_mappings(void) {
    FILE *maps = fopen("/proc/self/maps", "r");
    if (!maps) return 0;
    char line[8192]; int ok = 1;
    while (fgets(line, sizeof(line), maps)) {
        char *permissions = strchr(line, ' ');
        if (!permissions || strlen(++permissions) < 4 || permissions[3] == 's') {
            fprintf(stderr, "MIDAS_UNSUPPORTED shared mapping %s", line); ok = 0;
        }
    }
    fclose(maps); return ok;
}

/* Runs only after every traced task is dead. Never follows request symlinks. */
static int clean_tmp_fd(int fd) {
    DIR *dir = fdopendir(fd);
    if (!dir) { close(fd); return -1; }
    struct dirent *entry; int ok = 0;
    while ((entry = readdir(dir))) {
        const char *name = entry->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..")) continue;
        struct stat st;
        if (fstatat(fd, name, &st, AT_SYMLINK_NOFOLLOW) < 0) { ok = -1; break; }
        if (S_ISDIR(st.st_mode)) {
            if (fchmodat(fd, name, 0700, 0) < 0) { ok = -1; break; }
            int child = openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0 || clean_tmp_fd(child) < 0 || unlinkat(fd, name, AT_REMOVEDIR) < 0)
                { ok = -1; break; }
        } else if (unlinkat(fd, name, 0) < 0) { ok = -1; break; }
    }
    closedir(dir); return ok;
}

/* An explicit x86-64 syscall surface. Unknown operations trigger fresh fallback,
 * including ptrace, io_uring, IPC, namespace operations, and metadata changes
 * that Landlock cannot mediate. Register arguments cannot race other threads. */
static int admitted(uint64_t nr, const uint64_t *a) {
    switch (nr) {
    case SYS_read: case SYS_write: case SYS_readv: case SYS_writev:
    case SYS_pread64: case SYS_pwrite64: case SYS_close: case SYS_close_range:
    case SYS_open: case SYS_openat: case SYS_openat2:
    case SYS_stat: case SYS_fstat: case SYS_lstat: case SYS_newfstatat: case SYS_statx:
    case SYS_access: case SYS_faccessat: case SYS_faccessat2:
    case SYS_lseek: case SYS_getdents: case SYS_getdents64:
    case SYS_readlink: case SYS_readlinkat: case SYS_getcwd: case SYS_chdir: case SYS_fchdir:
    case SYS_mkdir: case SYS_mkdirat: case SYS_unlink: case SYS_unlinkat:
    case SYS_rmdir: case SYS_rename: case SYS_renameat: case SYS_renameat2:
    case SYS_link: case SYS_linkat: case SYS_symlink: case SYS_symlinkat:
    case SYS_ftruncate: case SYS_truncate: case SYS_fsync: case SYS_fdatasync:
    case SYS_statfs: case SYS_fstatfs: case SYS_umask:
    case SYS_mmap: case SYS_munmap: case SYS_mprotect: case SYS_mremap:
    case SYS_madvise: case SYS_brk:
    case SYS_futex: case SYS_futex_waitv: case SYS_set_robust_list: case SYS_rseq:
    case SYS_set_tid_address: case SYS_arch_prctl:
    case SYS_getpid: case SYS_getppid: case SYS_gettid: case SYS_getuid: case SYS_geteuid:
    case SYS_getgid: case SYS_getegid: case SYS_getgroups: case SYS_uname:
    case SYS_getsid: case SYS_getpgid: case SYS_getpgrp: case SYS_setsid:
    case SYS_getrlimit: case SYS_setrlimit: case SYS_getrusage: case SYS_times:
    case SYS_clock_gettime: case SYS_clock_getres: case SYS_gettimeofday:
    case SYS_nanosleep: case SYS_clock_nanosleep: case SYS_sched_yield:
    case SYS_rt_sigaction: case SYS_rt_sigprocmask: case SYS_sigaltstack:
    case SYS_rt_sigreturn: case SYS_restart_syscall: case SYS_rt_sigtimedwait:
    case SYS_rt_sigsuspend: case SYS_rt_sigpending:
    case SYS_kill: case SYS_tkill: case SYS_tgkill:
    case SYS_epoll_create: case SYS_epoll_create1: case SYS_epoll_ctl:
    case SYS_epoll_wait: case SYS_epoll_pwait: case SYS_epoll_pwait2:
    case SYS_eventfd: case SYS_eventfd2: case SYS_pipe: case SYS_pipe2:
    case SYS_dup: case SYS_dup2: case SYS_dup3: case SYS_fcntl:
    case SYS_poll: case SYS_ppoll: case SYS_select: case SYS_pselect6:
    case SYS_getrandom: case SYS_sysinfo: case SYS_getcpu:
    case SYS_fork: case SYS_vfork: case SYS_wait4: case SYS_waitid:
    case SYS_execve: case SYS_execveat: case SYS_exit: case SYS_exit_group:
        return 1;
    case SYS_clone:
        return !(a[0] & (CLONE_UNTRACED | CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID |
                         CLONE_NEWIPC | CLONE_NEWNET | CLONE_NEWUTS | CLONE_NEWCGROUP));
    case SYS_prlimit64: case SYS_sched_setaffinity: case SYS_sched_getaffinity:
        return a[0] == 0;
    case SYS_prctl:
        return a[0] == PR_SET_NAME || a[0] == PR_GET_NAME;
    case SYS_ioctl:
        return a[1] == TCGETS || a[1] == TIOCGWINSZ || a[1] == FIONREAD || a[1] == FIONBIO;
    default: return 0;
    }
}

#define MAX_TRACED 4096
static pid_t traced[MAX_TRACED];
static unsigned char attachment_stop[MAX_TRACED];
static size_t traced_count;
static void track(pid_t pid) {
    for (size_t i = 0; i < traced_count; i++) if (traced[i] == pid) return;
    if (traced_count == MAX_TRACED) die("trace task capacity");
    attachment_stop[traced_count] = 1;
    traced[traced_count++] = pid;
}
static int consume_attachment_stop(pid_t pid) {
    for (size_t i = 0; i < traced_count; i++) if (traced[i] == pid) {
        int initial = attachment_stop[i]; attachment_stop[i] = 0; return initial;
    }
    return 0;
}
static void untrack(pid_t pid) {
    for (size_t i = 0; i < traced_count; i++) if (traced[i] == pid) {
        traced[i] = traced[--traced_count];
        attachment_stop[i] = attachment_stop[traced_count]; return;
    }
}
static void kill_traced(void) {
    for (size_t i = 0; i < traced_count; i++) kill(traced[i], SIGKILL);
}

static unsigned supervise(pid_t child) {
    unsigned code = 125; int status, done = 0, unsupported = 0, ready = 0;
    unsigned long syscalls = 0, tasks = 1;
    traced_count = 0; track(child);
    if (waitpid(child, &status, 0) != child || !WIFSTOPPED(status)) die("initial trace stop");
    consume_attachment_stop(child);
    long opts = PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK |
                PTRACE_O_TRACECLONE | PTRACE_O_TRACEEXEC | PTRACE_O_EXITKILL;
    if (ptrace(PTRACE_SETOPTIONS, child, 0, opts) < 0 || ptrace(PTRACE_SYSCALL, child, 0, 0) < 0)
        die("start tracing");
    for (;;) {
        pid_t pid = waitpid(-1, &status, __WALL);
        if (pid < 0 && errno == EINTR) continue;
        if (pid < 0 && errno == ECHILD) break;
        if (pid < 0) die("trace wait");
        if (WIFEXITED(status) || WIFSIGNALED(status)) {
            untrack(pid);
            if (pid == child) {
                code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
                done = 1;
            }
            if (done || unsupported) kill_traced();
            continue;
        }
        track(pid);
        int signal = WSTOPSIG(status); unsigned event = (unsigned)status >> 16;
        if (event == PTRACE_EVENT_FORK || event == PTRACE_EVENT_VFORK || event == PTRACE_EVENT_CLONE) {
            unsigned long newborn;
            if (ptrace(PTRACE_GETEVENTMSG, pid, 0, &newborn) < 0) die("trace child id");
            track((pid_t)newborn); tasks++; signal = 0;
        } else if (event == PTRACE_EVENT_EXEC) {
            unsigned long old;
            if (ptrace(PTRACE_GETEVENTMSG, pid, 0, &old) < 0) die("trace exec id");
            if ((pid_t)old != pid) untrack((pid_t)old);
            signal = 0;
        } else if (signal == (SIGTRAP | 0x80)) {
            struct __ptrace_syscall_info info;
            if (ptrace(PTRACE_GET_SYSCALL_INFO, pid, sizeof(info), &info) < 0) die("syscall info");
            signal = 0;
            if (info.arch != AUDIT_ARCH_X86_64) unsupported = 1;
            if (info.op == PTRACE_SYSCALL_INFO_ENTRY) {
                syscalls++;
                if (!ready && (info.entry.nr == SYS_openat || info.entry.nr == SYS_open)) {
                    unsigned long address = info.entry.args[info.entry.nr == SYS_openat ? 1 : 0];
                    char path[256] = {0};
                    for (size_t offset = 0; offset + sizeof(long) < sizeof(path); offset += sizeof(long)) {
                        errno = 0;
                        long word = ptrace(PTRACE_PEEKDATA, pid, address + offset, 0);
                        if (errno) break;
                        memcpy(path + offset, &word, sizeof(word));
                        if (memchr(&word, 0, sizeof(word))) break;
                    }
                    fprintf(stderr, "MIDAS_RUNTIME_OPEN %s\n", path);
                }
                if ((info.entry.nr == SYS_clone3 || info.entry.nr == SYS_io_uring_setup) && !ready) {
                    /* Select libc/libuv's supported legacy paths during trusted
                     * startup. Never continue pointer-based clone3 or io_uring. */
                    struct user_regs_struct regs;
                    if (ptrace(PTRACE_GETREGS, pid, 0, &regs) < 0) die("clone3 regs");
                    regs.orig_rax = -1UL;
                    if (ptrace(PTRACE_SETREGS, pid, 0, &regs) < 0) die("clone3 fallback");
                } else if (!admitted(info.entry.nr, info.entry.args)) {
                    fprintf(stderr, "MIDAS_FRESH_REQUIRED syscall=%llu ready=%d\n",
                            (unsigned long long)info.entry.nr, ready); unsupported = 1;
                }
            } else if (info.op == PTRACE_SYSCALL_INFO_EXIT && info.exit.is_error &&
                       (info.exit.rval == -EACCES || info.exit.rval == -EPERM)) {
                fprintf(stderr, "MIDAS_FRESH_REQUIRED access=%lld\n", (long long)info.exit.rval);
                unsupported = 1;
            }
        } else if (signal == SIGUSR2 && pid == child && !ready) {
            ready = 1; signal = 0;
        } else if (signal == SIGSTOP) {
            if (consume_attachment_stop(pid)) signal = 0;
            else {
                /* A candidate's job-control stop must not be silently ignored.
                 * Re-run it with ordinary process semantics instead. */
                fprintf(stderr, "MIDAS_FRESH_REQUIRED request SIGSTOP\n");
                unsupported = 1;
            }
        } else if (signal == SIGTSTP || signal == SIGTTIN || signal == SIGTTOU) {
            fprintf(stderr, "MIDAS_FRESH_REQUIRED job-control signal=%d\n", signal);
            unsupported = 1;
        }
        if (done || unsupported) kill_traced();
        if (ptrace(PTRACE_SYSCALL, pid, 0, signal) < 0 && errno != ESRCH) die("trace continue");
    }
    fprintf(stderr, "MIDAS_TRACE tasks=%lu syscalls=%lu remaining=%zu ready=%d\n",
            tasks, syscalls, traced_count, ready);
    if (traced_count || !ready || unsupported) return 125;
    return code;
}

LEAN_EXPORT lean_obj_res midas_isolate(lean_obj_arg world) {
    (void)world;
    lean_finalize_task_manager();
    unsigned count = thread_count();
    fprintf(stderr, "MIDAS_THREAD_GUARD count=%u\n", count);
    char cwd[4096];
    if (count != 1 || !descriptors_clean() || !empty_directory("/tmp") || !private_mappings() ||
        !getcwd(cwd, sizeof(cwd)) || strcmp(cwd, "/project")) {
        errno = EBUSY; return error("unsupported import snapshot; use fresh process");
    }
    fflush(NULL);
    pid_t child = fork();
    if (child < 0) return error("request fork");
    if (child == 0) {
        close(control_fd); control_fd = -1;
        int out = open("/proc/self/fd/1", O_WRONLY | O_CLOEXEC);
        int err = open("/proc/self/fd/2", O_WRONLY | O_CLOEXEC);
        if (out < 0 || err < 0 || dup2(out, 1) < 0 || dup2(err, 2) < 0) die("diagnostics");
        close(out); close(err);
        if (!freopen("/dev/null", "r", stdin)) die("stdin");
        if (syscall(SYS_close_range, 3, ~0U, 0) < 0) die("descriptors");
        restrict_request();
        /* WorkerContext supervises the complete request tree against the job's
         * wall budget, just as for fresh launches. A private CPU cap would kill
         * valid longer requests and waste their work on a fresh fallback. */
        struct rlimit core = {0, 0};
        if (setrlimit(RLIMIT_CORE, &core) < 0) die("limits");
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) < 0) die("trace me");
        raise(SIGSTOP);
        request_child = 1;
        __real_initialize_libuv();
        lean_init_task_manager();
        raise(SIGUSR2);
        return lean_io_result_mk_ok(lean_box_uint32(0));
    }
    unsigned code = supervise(child);
    if (chmod("/tmp", 01777) < 0 || clean_tmp_fd(open("/tmp", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)) < 0)
        return error("temporary cleanup; discard worker");
    lean_init_task_manager();
    return lean_io_result_mk_ok(lean_box_uint32(code + 1));
}

LEAN_EXPORT lean_obj_res midas_child_exit(uint32_t code, lean_obj_arg world) {
    (void)world; fflush(NULL); _exit(code);
}
