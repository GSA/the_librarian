#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/types.h>
#include <limits.h>
static unsigned long n_fsync, n_fdatasync, n_write, n_pwrite;
static int (*r_fsync)(int); static int (*r_fdatasync)(int);
static ssize_t (*r_write)(int,const void*,size_t); static ssize_t (*r_pwrite)(int,const void*,size_t,off_t);
static ssize_t (*r_pwrite64)(int,const void*,size_t,off_t);
static FILE *logf; static int busy;
static void note(const char*kind,int fd){
  if(busy||!logf) return; busy=1; char l[64],p[PATH_MAX]; snprintf(l,sizeof l,"/proc/self/fd/%d",fd);
  ssize_t n=readlink(l,p,sizeof p-1); if(n<0){strcpy(p,"?");} else p[n]=0; fprintf(logf,"%s %s\n",kind,p); busy=0;}
__attribute__((constructor)) static void init(void){
  r_fsync=dlsym(RTLD_NEXT,"fsync"); r_fdatasync=dlsym(RTLD_NEXT,"fdatasync");
  r_write=dlsym(RTLD_NEXT,"write"); r_pwrite=dlsym(RTLD_NEXT,"pwrite"); r_pwrite64=dlsym(RTLD_NEXT,"pwrite64");}
void shim_log_open(const char*path){ if(logf) fclose(logf); logf=path?fopen(path,"w"):NULL; if(logf) setvbuf(logf,NULL,_IOFBF,1<<20);}
void shim_log_close(void){ if(logf){fclose(logf);logf=NULL;}}
int fsync(int fd){n_fsync++; note("fsync",fd); return r_fsync(fd);}
int fdatasync(int fd){n_fdatasync++; note("fdatasync",fd); return r_fdatasync(fd);}
ssize_t write(int fd,const void*b,size_t n){n_write++; return r_write(fd,b,n);}
ssize_t pwrite(int fd,const void*b,size_t n,off_t o){n_pwrite++; return r_pwrite(fd,b,n,o);}
ssize_t pwrite64(int fd,const void*b,size_t n,off_t o){n_pwrite++; return r_pwrite64(fd,b,n,o);}
unsigned long shim_fsync(void){return n_fsync;}
unsigned long shim_fdatasync(void){return n_fdatasync;}
unsigned long shim_pwrite(void){return n_pwrite;}
unsigned long shim_write(void){return n_write;}
