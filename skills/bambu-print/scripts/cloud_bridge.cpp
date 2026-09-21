// Uses the installed official SDK, with the matching public 02.08.02.54 ABI.
// Authentication setup and state queries precede any one-shot print dispatch.
#include "vendor/bambu_networking.hpp"
#include "vendor/json.hpp"
#include <dlfcn.h>
#include <fstream>
#include <iostream>
#include <thread>
#include <chrono>
#include <mutex>
#include <deque>
#include <atomic>
#include <sys/resource.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
using json=nlohmann::json;
using namespace BBL;
using Clock=std::chrono::steady_clock;
template<class T>T api(void*h,const char*n){auto p=dlsym(h,n);if(!p){std::cerr<<"Missing API "<<n<<std::endl;std::_Exit(2);}return reinterpret_cast<T>(p);}
std::mutex mutex_,output_mutex;
std::deque<std::function<void()>> pending;
json state=json::object(),receipt=json::object();
Clock::time_point last_state{};
void event(const json&j){std::lock_guard<std::mutex>g(output_mutex);std::cout<<"EVENT "<<j.dump()<<std::endl;}
void pump(){std::deque<std::function<void()>> q;{std::lock_guard<std::mutex>g(mutex_);q.swap(pending);}for(auto&f:q)f();}
json snapshot(){std::lock_guard<std::mutex>g(mutex_);return state;}
int main(int argc,char**argv){
    umask(0077);rlimit limit{0,0};setrlimit(RLIMIT_CORE,&limit);
    if(argc!=3)return 2;
    const std::string session=argv[1],mode=argv[2];
    if(mode!="status"&&mode!="print")return 2;
    json opts;std::ifstream(session+"/options.json")>>opts;
    std::string dev=opts.value("printer","");
    const std::string country_code=opts.at("country_code");
    void*h=dlopen(opts.at("plugin_path").get<std::string>().c_str(),RTLD_NOW|RTLD_LOCAL);
    if(!h)return 2;
    auto version=api<std::string(*)()>(h,"bambu_network_get_version")();
    event({{"sdk_version",version},{"mode",mode}});
    if(version!=BAMBU_NETWORK_AGENT_VERSION){event({{"error","SDK version differs from bundled ABI"},{"expected",BAMBU_NETWORK_AGENT_VERSION}});return 3;}
    auto create=api<void*(*)(std::string)>(h,"bambu_network_create_agent");
    auto set_config=api<int(*)(void*,std::string)>(h,"bambu_network_set_config_dir");
    auto set_cert=api<int(*)(void*,std::string,std::string)>(h,"bambu_network_set_cert_file");
    auto country=api<int(*)(void*,std::string)>(h,"bambu_network_set_country_code");
    auto send=api<int(*)(void*,std::string,std::string,int,int)>(h,"bambu_network_send_message");
    auto install_cert=api<void(*)(void*,std::string,bool)>(h,"bambu_network_install_device_cert");
    void*a=create(session);if(!a)return 3;
    set_config(a,session);api<int(*)(void*)>(h,"bambu_network_init_log")(a);
    set_cert(a,opts.at("cert_dir"),"slicer_base64.cer");
    std::map<std::string,std::string>headers{{"X-BBL-Client-Type","slicer"},{"X-BBL-Client-Name","BambuStudio"},{"X-BBL-Client-Version","02.08.02.60"},{"X-BBL-OS-Type","linux"},{"X-BBL-Language","zh-cn"}};
    if(opts.contains("slicer_uuid"))headers["X-BBL-Device-ID"]=opts["slicer_uuid"];
    api<int(*)(void*,std::map<std::string,std::string>)>(h,"bambu_network_set_extra_http_header")(a,headers);
    api<int(*)(void*,GetCountryCodeFn)>(h,"bambu_network_set_get_country_code_fn")(a,[country_code]{return country_code;});
    api<int(*)(void*,QueueOnMainFn)>(h,"bambu_network_set_queue_on_main_fn")(a,[](std::function<void()>f){std::lock_guard<std::mutex>g(mutex_);pending.push_back(std::move(f));});
    api<int(*)(void*,OnServerConnectedFn)>(h,"bambu_network_set_on_server_connected_fn")(a,[](int code,int reason){event({{"mqtt_connected_code",code},{"reason_code",reason}});});
    auto request_status=[a,&dev,send]{
        json p={{"pushing",{{"sequence_id","9032101"},{"command","pushall"},{"version",1},{"push_target",1}}}};
        event({{"status_request_rc",send(a,dev,p.dump(),0,0)}});
    };
    api<int(*)(void*,OnPrinterConnectedFn)>(h,"bambu_network_set_on_printer_connected_fn")(a,[request_status,install_cert,a,&dev](std::string){
        request_status();install_cert(a,dev,false);event({{"official_device_cert_setup","requested"}});
    });
    api<int(*)(void*,OnMessageFn)>(h,"bambu_network_set_on_message_fn")(a,[&dev](std::string id,std::string msg){
        if(id!=dev&&id!="tunnel/"+dev)return;
        auto j=json::parse(msg,nullptr,false);if(j.is_discarded())return;
        if(j.contains("security")&&j["security"].is_object()){
            json safe=json::object();for(const char*k:{"command","result","reason"})if(j["security"].contains(k))safe[k]=j["security"][k];
            event({{"security_result",safe}});
        }
        if(j.contains("print")&&j["print"].is_object()){
            const auto&p=j["print"];json safe=json::object();
            for(const char*k:{"gcode_state","gcode_file","subtask_name","subtask_id","task_id","project_id","profile_id","job_id","mc_percent","mc_remaining_time","print_error","sdcard","nozzle_diameter","nozzle_type","nozzle_temper","nozzle_target_temper","bed_temper","bed_target_temper","layer_num","total_layer_num","stg_cur","mc_print_stage","mc_print_sub_stage","command","result","reason","ams_status","ams_rfid_status","vt_tray","hms"})
                if(p.contains(k))safe[k]=p[k];
            if(safe.contains("vt_tray")&&safe["vt_tray"].is_object()){
                json tray=json::object();
                for(const char*k:{"id","tray_type","tray_info_idx","tray_color","nozzle_temp_min","nozzle_temp_max","k","cali_idx"})
                    if(safe["vt_tray"].contains(k))tray[k]=safe["vt_tray"][k];
                safe["vt_tray"]=tray;
            }
            {std::lock_guard<std::mutex>g(mutex_);state.update(safe);if(p.contains("gcode_state"))last_state=Clock::now();}
            if(!safe.empty())event({{"printer",safe}});
        }
    });
    country(a,country_code);api<int(*)(void*)>(h,"bambu_network_start")(a);
    if(!api<bool(*)(void*)>(h,"bambu_network_is_user_login")(a)){event({{"error","no saved login"}});std::_Exit(3);}
    unsigned http=0;std::string body;
    int rc=api<int(*)(void*,unsigned*,std::string*)>(h,"bambu_network_get_user_print_info")(a,&http,&body);
    event({{"cloud_query_rc",rc},{"http_status",http}});
    auto bound=json::parse(body,nullptr,false);json matches=json::array();
    if(rc==0&&http==200&&!bound.is_discarded()&&bound.contains("devices"))for(auto&d:bound["devices"])
        if(d.value("dev_id","")==dev||d.value("dev_name","")==dev)matches.push_back(d);
    if(matches.size()!=1||matches[0].value("dev_model_name","")!="C12"||!matches[0].value("dev_online",false)){
        event({{"error","select exactly one bound, online P1S by name or serial"}});std::_Exit(4);
    }
    dev=matches[0].at("dev_id");const std::string dev_name=matches[0].value("dev_name",dev);
    event({{"device",{{"name",dev_name},{"serial",dev},{"model","P1S"}}}});
    // Match GUI_App::post_init -> check_cert. The official SDK manages app
    // signing certificates internally; no keys are extracted or patched.
    int cert_rc=api<int(*)(void*)>(h,"bambu_network_update_cert")(a);
    event({{"official_app_cert_update_rc",cert_rc}});
    if(cert_rc!=0){event({{"error","official app certificate refresh failed"}});std::_Exit(4);}
    event({{"connect_server_rc",api<int(*)(void*)>(h,"bambu_network_connect_server")(a)}});
    const auto connected=api<bool(*)(void*)>(h,"bambu_network_is_server_connected");
    auto begin=Clock::now();
    while(!connected(a)&&Clock::now()-begin<std::chrono::seconds(25)){pump();std::this_thread::sleep_for(std::chrono::milliseconds(100));}
    if(!connected(a)){event({{"error","cloud MQTT unavailable"}});std::_Exit(5);}
    api<int(*)(void*,std::string)>(h,"bambu_network_set_user_selected_machine")(a,dev);
    event({{"subscribe_rc",api<int(*)(void*,std::vector<std::string>)>(h,"bambu_network_add_subscribe")(a,{dev})}});
    api<int(*)(void*,std::string)>(h,"bambu_network_start_subscribe")(a,"app");
    begin=Clock::now();bool asked=false;
    while(Clock::now()-begin<std::chrono::seconds(30)){
        pump();
        if(!asked&&Clock::now()-begin>std::chrono::seconds(2)){request_status();asked=true;}
        if(snapshot().contains("gcode_state")&&Clock::now()-begin>std::chrono::seconds(8))break;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    auto before=snapshot();event({{"preflight",before}});
    bool fresh;{std::lock_guard<std::mutex>g(mutex_);fresh=Clock::now()-last_state<std::chrono::seconds(15);}
    if(!before.contains("gcode_state")||!fresh){
        event({{"error","no fresh printer state"}});std::_Exit(6);
    }
    if(mode=="status")std::_Exit(0);
    const std::string gs=before.value("gcode_state","");
    if(gs!="IDLE"&&gs!="FINISH"){event({{"error","printer is not idle"}});std::_Exit(7);}
    if(!before.contains("print_error")||before["print_error"]!=0||!before.contains("hms")||!before["hms"].empty()){
        event({{"error","printer reports an error or alert, or error state is unknown"}});std::_Exit(7);
    }
    if(!before.value("sdcard",false)){event({{"error","SD card unavailable"}});std::_Exit(7);}
    if(before.value("nozzle_diameter","")!="0.4"||!before.contains("vt_tray")||before["vt_tray"].value("tray_type","")!="PLA"){
        event({{"error","0.4 mm nozzle and external PLA are required"}});std::_Exit(7);
    }
    PrintParams p{};
    p.dev_id=dev;p.dev_name=dev_name;
    p.task_name=opts.at("task_name");p.project_name=p.task_name;p.preset_name="P1S external PLA";
    p.filename=opts.at("filename");p.config_filename=opts.at("config_filename");p.plate_index=1;
    p.connection_type="cloud";p.print_type="from_normal";
    p.username="bblp";p.use_ssl_for_ftp=true;p.use_ssl_for_mqtt=true;
    p.task_bed_leveling=opts.at("bed_leveling");p.task_flow_cali=false;p.task_vibration_cali=false;p.task_layer_inspect=false;
    p.task_record_timelapse=opts.at("timelapse");p.task_timelapse_use_internal=false;p.task_use_ams=false;
    p.task_bed_type=opts.at("bed_type");p.task_ext_change_assist=false;p.try_emmc_print=false;
    p.ams_mapping="[254]";p.ams_mapping2="";p.slicer_uid=opts.value("slicer_uuid","");
    if(!std::ifstream(p.filename)||!std::ifstream(p.config_filename)){event({{"error","job file missing"}});std::_Exit(8);}
    // Durable marker precedes the only call that can start a print. Never auto retry.
    const std::string marker=opts.at("attempt_marker");
    int fd=open(marker.c_str(),O_WRONLY|O_CREAT|O_EXCL,0600);
    if(fd<0){event({{"error","dispatch marker exists or cannot be created; inspect before retry"}});std::_Exit(9);}
    auto mark=json({{"task_name",p.task_name},{"dev_id",dev},{"dispatch_attempted",true}}).dump(2);
    bool persisted=write(fd,mark.data(),mark.size())==static_cast<ssize_t>(mark.size())&&fsync(fd)==0;
    close(fd);
    if(!persisted){event({{"error","could not persist dispatch marker"}});std::_Exit(9);}
    std::atomic<bool>done=false;int print_rc=-999;
    std::thread worker([&]{
        auto update=[&](int stage,int code,std::string msg){
            {std::ofstream f(session+"/sdk_print_messages.jsonl",std::ios::app);f<<json({{"stage",stage},{"code",code},{"message",msg}}).dump()<<'\n';}
            event({{"dispatch_stage",stage},{"stage_code",code}});
        };
        auto wait=[&](int stage,std::string info){
            auto j=json::parse(info,nullptr,false);json safe=json::object();
            if(j.is_object())for(const char*k:{"job_id","task_id","subtask_id","project_id","profile_id"})if(j.contains(k))safe[k]=j[k];
            {std::lock_guard<std::mutex>g(mutex_);receipt.update(safe);}
            event({{"cloud_receipt",safe}});return true;
        };
        print_rc=api<int(*)(void*,PrintParams,OnUpdateStatusFn,WasCancelledFn,OnWaitFn)>(h,"bambu_network_start_print")(a,p,update,[]{return false;},wait);
        done=true;
    });
    while(!done){pump();std::this_thread::sleep_for(std::chrono::milliseconds(100));}
    worker.join();event({{"start_print_rc",print_rc}});
    // Upload success is not proof of a printer start: look for matching task state.
    begin=Clock::now();bool confirmed=false;
    while(Clock::now()-begin<std::chrono::seconds(55)){
        pump();auto s=snapshot();
        const auto st=s.value("gcode_state","");
        json rec;{std::lock_guard<std::mutex>g(mutex_);rec=receipt;}
        auto as_id=[](const json&j){return j.is_string()?j.get<std::string>():j.dump();};
        bool id_matches=rec.contains("job_id")&&s.contains("subtask_id")&&as_id(rec["job_id"])==as_id(s["subtask_id"]);
        if((st=="PREPARE"||st=="RUNNING")&&s.value("subtask_name","")==p.task_name&&id_matches){confirmed=true;break;}
        if(print_rc!=0)break;
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    json result={{"start_print_rc",print_rc},{"printer_start_confirmed",confirmed},{"state",snapshot()},{"task_name",p.task_name},{"use_ams",false}};
    {std::lock_guard<std::mutex>g(mutex_);result["cloud_receipt"]=receipt;}
    event({{"dispatch_result",result}});
    std::ofstream(opts.at("receipt_file").get<std::string>())<<result.dump(2)<<'\n';
    std::_Exit(confirmed?0:10);
}
