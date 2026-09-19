/* Private NAS client; CLI subscription 1/2, only client bind0x45 and read0x43.
 * ABI/schema: Qualcomm-generated qmi_client.h, network_access_service_v01.h/.c
 * https://github.com/bcyj/android_tools_leeco_msm8996/tree/master/qmi
 * qmi-framework/inc/qmi_client.h in same repository. Bind affects this client.
 * init_instance os_params=NULL verified on RG650V native libqmi_cci.
 */
#include <dlfcn.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc,char **argv){
 if(argc!=2 || (argv[1][0]!='1' && argv[1][0]!='2') || argv[1][1]) return 2;
 alarm(15);
 void *s=dlopen("libqmiservices.so",RTLD_NOW|RTLD_GLOBAL);
 void *l=dlopen("libqmi_cci.so",RTLD_NOW|RTLD_GLOBAL);
 if(!s||!l){fprintf(stderr,"load %s\n",dlerror());return 3;}
 void *(*obj)(int,int,int)=dlsym(s,"nas_get_service_object_internal_v01");
 int (*init)(void*,unsigned,void*,void*,void*,uint32_t,void**)=dlsym(l,"qmi_client_init_instance");
 int (*raw)(void*,unsigned,void*,unsigned,void*,unsigned,unsigned*,unsigned)=dlsym(l,"qmi_client_send_raw_msg_sync");
 int (*release)(void*)=dlsym(l,"qmi_client_release");
 if(!obj||!init||!raw||!release)return 4;
 void *service=NULL,*client=NULL;
 for(int minor=0;minor<512&&!service;minor++){service=obj(1,minor,6);if(service)printf("service_minor=%d\n",minor);}
 if(!service)return 5;
 int r=init(service,0xffff,NULL,NULL,NULL,3000,&client);
 printf("init=%d\n",r);if(r)return 6;
 unsigned char bind[]={1,1,0,(unsigned char)(argv[1][0]-'1')},buf[65536];unsigned n=0;
 r=raw(client,0x45,bind,sizeof(bind),buf,sizeof(buf),&n,3000);
 printf("bind rc=%d data=",r);for(unsigned i=0;i<n;i++)printf("%02x",buf[i]);puts("");
 int ok=0;for(unsigned i=0;i+3<=n;){unsigned len=buf[i+1]|buf[i+2]<<8;if(i+3+len>n)break;if(buf[i]==2&&len==4&&buf[i+3]==0&&buf[i+4]==0)ok=1;i+=3+len;}
 if(!r&&ok){n=0;r=raw(client,0x43,NULL,0,buf,sizeof(buf),&n,3000);printf("cell rc=%d data=",r);for(unsigned i=0;i<n;i++)printf("%02x",buf[i]);puts("");}
 printf("release=%d\n",release(client));return 0;
}
