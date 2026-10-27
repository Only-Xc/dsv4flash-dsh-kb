"""Fixed receipts used when model-based conversion cannot be trusted."""

MESSAGES = {
    "en": {
        "failure": "Something went wrong while processing your request. Please try again shortly.",
        "clarify": "Could you clarify what you would like help with?",
        "progress": "I am processing your request…",
        "cancelled": "The response was stopped. Please retry when ready.",
        "partial": "The answer is incomplete. Please retry.",
    },
    "ar": {
        "failure": "حدثت مشكلة أثناء معالجة طلبك. يرجى المحاولة مرة أخرى بعد قليل.",
        "clarify": "يرجى توضيح ما تريد المساعدة بشأنه.",
        "progress": "أعمل على معالجة طلبك…",
        "cancelled": "تم إيقاف الرد. يمكنك إعادة المحاولة عندما تكون مستعداً.",
        "partial": "الرد غير مكتمل. يرجى إعادة المحاولة.",
    },
    "ur": {
        "failure": "آپ کی درخواست پر کارروائی کے دوران مسئلہ پیش آیا۔ براہِ کرم کچھ دیر بعد دوبارہ کوشش کریں۔",
        "clarify": "براہِ کرم واضح کریں کہ آپ کو کس چیز میں مدد چاہیے۔",
        "progress": "میں آپ کی درخواست پر کام کر رہا ہوں…",
        "cancelled": "جواب روک دیا گیا ہے۔ جب تیار ہوں تو دوبارہ کوشش کریں۔",
        "partial": "جواب نامکمل ہے۔ براہ کرم دوبارہ کوشش کریں۔",
    },
    "ml": {
        "failure": "നിങ്ങളുടെ അഭ്യർത്ഥന കൈകാര്യം ചെയ്യുന്നതിനിടെ ഒരു പ്രശ്നമുണ്ടായി. ദയവായി അൽപ്പസമയത്തിന് ശേഷം വീണ്ടും ശ്രമിക്കുക.",
        "clarify": "നിങ്ങൾക്ക് എന്ത് സഹായമാണ് വേണ്ടതെന്ന് ദയവായി വ്യക്തമാക്കുക.",
        "progress": "നിങ്ങളുടെ അഭ്യർത്ഥന പരിശോധിക്കുകയാണ്…",
        "cancelled": "മറുപടി നിർത്തി. തയ്യാറാകുമ്പോൾ വീണ്ടും ശ്രമിക്കുക.",
        "partial": "മറുപടി അപൂർണ്ണമാണ്. ദയവായി വീണ്ടും ശ്രമിക്കുക.",
    },
    "tl": {
        "failure": "Nagkaroon ng problema sa pagproseso ng iyong kahilingan. Pakisubukang muli pagkaraan ng ilang sandali.",
        "clarify": "Pakilinaw kung ano ang nais mong maitutulong ko.",
        "progress": "Pinoproseso ko ang iyong kahilingan…",
        "cancelled": "Itinigil ang sagot. Subukang muli kapag handa ka na.",
        "partial": "Hindi kumpleto ang sagot. Pakisubukang muli.",
    },
}


def language_message(language: str, kind: str) -> str:
    """Return a bounded, prewritten receipt; never invent business facts."""
    return MESSAGES.get(language, MESSAGES["en"])[kind]
