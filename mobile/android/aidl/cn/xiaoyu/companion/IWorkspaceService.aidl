// 副屏助手（shell 身份）与小鱼 App 之间的最小接口。
// 一个 JSON 字符串进、一个 JSON 字符串出：副屏的建屏、取帧、定向输入都在这后面，
// 避免把每个动作都做成一个 AIDL 方法。
package cn.xiaoyu.companion;

interface IWorkspaceService {
    String request(String json);
}
