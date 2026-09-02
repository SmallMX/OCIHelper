"""
安全规则协议枚举模块

对应 Java 原项目：com.yohann.ocihelper.enums.SecurityRuleProtocolEnum
功能：定义 OCI 安全列表规则中支持的所有网络协议及其编号。
"""

from enum import Enum
from typing import Optional


class SecurityRuleProtocol(Enum):
    """
    安全规则协议枚举

    对应 Java 原项目中的 SecurityRuleProtocolEnum。
    协议编号遵循 IANA 协议编号标准（https://www.iana.org/assignments/protocol-numbers）。

    OCI 安全列表中使用这些编号来标识允许或拒绝的网络协议类型。
    """

    # (协议编号, 协议描述)
    ALL = ("all", "所有协议")
    ICMP = ("1", "ICMP")
    TCP = ("6", "TCP")
    UDP = ("17", "UDP")
    IGMP = ("2", "IGMP")
    GGP = ("3", "GGP")
    IP_IN_IP = ("4", "IP-in-IP")
    ST = ("5", "ST")
    CBT = ("7", "CBT")
    EGP = ("8", "EGP")
    IGP = ("9", "IGP")
    BBN_RCC_MON = ("10", "BBN-RCC-MON")
    NVP_II = ("11", "NVP-II")
    PUP = ("12", "PUP")
    ARGUS = ("13", "ARGUS")
    EMCON = ("14", "EMCON")
    CHAOS = ("16", "CHAOS")
    MUX = ("18", "MUX")
    DCN_MEAS = ("19", "DCN-MEAS")
    HMP = ("20", "HMP")
    PRM = ("21", "PRM")
    TRUNK_1 = ("23", "TRUNK-1")
    TRUNK_2 = ("24", "TRUNK-2")
    LEAF_1 = ("25", "LEAF-1")
    LEAF_2 = ("26", "LEAF-2")
    RDP = ("27", "可靠的数据协议")
    IRTP = ("28", "IRTP")
    ISO_TP4 = ("29", "ISO-TP4")
    NETBLT = ("30", "NETBLT")
    MFE_NSP = ("31", "MFE-NSP")
    MERIT_INP = ("32", "MERIT-INP")
    DCCP = ("33", "DCCP")
    THREE_PC = ("34", "3PC")
    IDPR = ("35", "IDPR")
    DDP = ("37", "DDP")
    IDPR_CMTP = ("38", "IDPR-CMTP")
    TP_PLUS_PLUS = ("39", "TP++")
    IL = ("40", "IL")
    IPV6 = ("41", "IPv6")
    SDRP = ("42", "SDRP")
    IPV6_ROUTE = ("43", "IPv6-Route")
    IPV6_FRAG = ("44", "IPv6-Frag")
    IDRP = ("45", "IDRP")
    RSVP = ("46", "RSVP")
    GRE = ("47", "GRE")
    MHRP = ("48", "MHRP")
    BNA = ("49", "BNA")
    ESP = ("50", "ESP")
    AH = ("51", "AH")
    I_NLSP = ("52", "I-NLSP")
    SWIPE = ("53", "SWIPE")
    NARP = ("54", "NARP")
    MOBILE = ("55", "MOBILE")
    TLSP = ("56", "TLSP")
    SKIP = ("57", "SKIP")
    IPV6_ICMP = ("58", "IPv6-ICMP")
    IPV6_NONXT = ("59", "IPv6-NoNxt")
    IPV6_OPTS = ("60", "IPv6-Opts")
    ANY_HOST_INTERNAL = ("61", "任何主机内部协议")
    CFTP = ("62", "CFTP")
    ANY_LOCAL_NETWORK = ("63", "任何本地网络")
    SAT_EXPAK = ("64", "SAT-EXPAK")
    KRYPTOLAN = ("65", "KRYPTOLAN")
    RVD = ("66", "RVD")
    IPPC = ("67", "IPPC")
    ANY_DFS = ("68", "任何分布式文件系统")
    SAT_MON = ("69", "SAT-MON")
    IPCU = ("71", "IPCU")
    CPNX = ("72", "CPNX")
    CPHB = ("73", "CPHB")
    PVP = ("75", "PVP")
    BR_SAT_MON = ("76", "BR-SAT-MON")
    SUN_ND = ("77", "SUN-ND")
    ISO_IP = ("80", "ISO-IP")
    SECURE_VMTP = ("82", "SECURE-VMTP")
    TTP_IPTM = ("84", "TTP/IPTM")
    NSFNET_IGP = ("85", "NSFNET-IGP")
    DGP = ("86", "DGP")
    TCF = ("87", "TCF")
    EIGRP = ("88", "EIGRP")
    OSPF = ("89", "OSPF")
    SPRITE_RPC = ("90", "Sprite-RPC")
    LARP = ("91", "LARP")
    MTP = ("92", "MTP")
    AX_25 = ("93", "AX.25")
    IPIP = ("94", "IPIP")
    MICP = ("95", "MICP")
    SCC_SP = ("96", "SCC-SP")
    ETHERIP = ("97", "ETHERIP")
    ENCAP = ("98", "ENCAP")
    ANY_PRIVATE_ENC = ("99", "任何专用加密方案")
    GMTP = ("100", "GMTP")
    IFMP = ("101", "IFMP")
    PNNI = ("102", "PNNI")
    PIM = ("103", "PIM")
    ARIS = ("104", "ARIS")
    SCPS = ("105", "SCPS")
    QNX = ("106", "QNX")
    A_N = ("107", "A/N")
    IPCOMP = ("108", "IPComp")
    SNP = ("109", "SNP")
    COMPAQ_PEER = ("110", "Compaq-Peer")
    IPX_IN_IP = ("111", "IPX-in-IP")
    PGM = ("113", "PGM")
    ANY_ZERO_HOP = ("114", "任意 0 跃点协议")
    L2TP = ("115", "L2TP")
    DDX = ("116", "DDX")
    IATP = ("117", "IATP")
    STP = ("118", "STP")
    SRP = ("119", "SRP")
    SMP = ("121", "SMP")
    SM = ("122", "SM")
    PTP = ("123", "PTP")
    IS_IS = ("124", "IS-IS")
    FIRE = ("125", "FIRE")
    CRTP = ("126", "CRTP")
    CRUDP = ("127", "CRUDP")
    SSCOPMCE = ("128", "SSCOPMCE")
    IPLT = ("129", "IPLT")
    SPS = ("130", "SPS")
    PIPE = ("131", "PIPE")
    SCTP = ("132", "SCTP")
    FC = ("133", "FC")
    RSVP_E2E_IGNORE = ("134", "RSVP-E2E-IGNORE")
    MOBILITY = ("135", "Mobility")
    UDPLITE = ("136", "UDPLite")
    MPLS_IN_IP = ("137", "MPLS-in-IP")
    MANET = ("138", "Manet")
    HIP = ("139", "HIP")
    SHIM6 = ("140", "Shim6")
    ROHC = ("142", "ROHC")

    def __init__(self, code: str, desc: str):
        """
        初始化安全规则协议枚举

        参数:
            code: IANA 协议编号（"all" 表示所有协议）
            desc: 协议描述
        """
        self.code = code
        self.desc = desc

    @classmethod
    def from_code(cls, code: str) -> Optional["SecurityRuleProtocol"]:
        """
        根据协议编号查找枚举实例

        对应 Java: SecurityRuleProtocolEnum.fromCode(code)

        参数:
            code: 协议编号

        返回:
            匹配的枚举实例

        异常:
            ValueError: 编号无效时抛出
        """
        for protocol in cls:
            if protocol.code == code:
                return protocol
        raise ValueError(f"无效的协议编号: {code}")
