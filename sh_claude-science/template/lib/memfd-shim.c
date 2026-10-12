/*
 * memfd_create() for glibc older than 2.27
 *
 * With the sandbox on, claude-science writes each sandbox's mount manifest to a
 * sealed memfd, and looks memfd_create() up at runtime: dlopen("libc.so.6")
 * first, then dlopen("libc.so"). Sherlock's glibc 2.17 predates the wrapper
 * (the kernel has the syscall), and its libc.so is a linker script, so both
 * fail. script.sh compiles this into a library named libc.so and puts it on
 * LD_LIBRARY_PATH, which the second lookup then finds. fcntl(), which
 * claude-science looks up from the same handle, resolves through this
 * library's dependency on the real libc.
 */

#define _GNU_SOURCE
#include <unistd.h>
#include <sys/syscall.h>

#ifndef SYS_memfd_create
#define SYS_memfd_create 319 /* x86_64 */
#endif

int memfd_create(const char *name, unsigned int flags)
{
    return (int) syscall(SYS_memfd_create, name, flags);
}
