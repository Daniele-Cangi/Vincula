#include "common/Settings.h"
#include "server/Config.h"
#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextStream>
#include <fstream>
int main(int argc,char**argv){
 QCoreApplication app(argc,argv);
 if(argc!=3)return 2;
 try{
  Settings::setSettingsFile(QString::fromUtf8(argv[1]));
  deskflow::server::Config config(nullptr);
  std::ifstream input(argv[2]);if(!input)return 3;input>>config;
  QJsonArray computers;QJsonObject links;
  const Direction directions[]={Direction::Left,Direction::Right,Direction::Top,Direction::Bottom};
  const char* names[]={"left","right","up","down"};
  for(auto it=config.begin();it!=config.end();++it){
   const auto name=*it;computers.append(QString::fromStdString(name));QJsonObject neighbors;
   for(int i=0;i<4;++i){auto neighbor=config.getNeighbor(name,directions[i],0.5f,nullptr);
    if(!neighbor.empty())neighbors[names[i]]=QString::fromStdString(neighbor);}
   links[QString::fromStdString(name)]=neighbors;
  }
  QTextStream(stdout)<<QJsonDocument(QJsonObject{{"computers",computers},{"links",links}}).toJson(QJsonDocument::Compact)<<Qt::endl;
  return 0;
 }catch(const std::exception& e){QTextStream(stderr)<<e.what()<<Qt::endl;return 4;}
}
