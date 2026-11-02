$(document).ready(function () {
    // console.log("In ready....");
    // Load Left contents
    loadContent();

    $("#claimprofile-content-en").hide();
    $("#claimprofile-content-he").hide();
    $("#claimprofile-content-ar").hide();

    $("#claimprofile-content-sq-AL").hide();
    $("#claimprofile-content-hy-AM").hide();
    $("#claimprofile-content-az-Cyrl-AZ").hide();
    $("#claimprofile-content-bs-Latn-BA").hide();
    $("#claimprofile-content-hr-HR").hide();
    $("#claimprofile-content-fa-IR").hide();
    $("#claimprofile-content-mk-MK").hide();
    $("#claimprofile-content-el-GR").hide();
    $("#claimprofile-content-ru-RU").hide();
    $("#claimprofile-content-sr-Latn-CS").hide();
    $("#claimprofile-content-tr-TR").hide();

    // Provide captcha images and custom links for signin/signup
    // console.log("In ready....");
    if(document.getElementById("password")) {
        let locale = findLocale();

        //console.log("Locale value extracted is: ", locale);
        setCustomLinks(locale);
    }
    else if (document.getElementById("continue")) {
        setCustomLinksForClaiming(findLocale());
    }
});

var gIsAudioLoaded = false;

function loadContent() {
      var includes = $('[data-include]');
      $.each(includes, function () {
        var file = 'https://atlasportalauthsa.z2.web.core.usgovcloudapi.net/B2C/assets/html/' + $(this).data('include') + '.html';
        $(this).load(file);
      });
};

function setCustomLinks(locale) {
    // console.log("Setting up custom links");
    // console.log("window.SETTINGS.config.passwordResetLink: " + window.SETTINGS.config.passwordResetLink)
    // console.log("window.SETTINGS.config.signupLink: " + window.SETTINGS.config.signupLink)
    let signupLink = window.SETTINGS.config.signupLink.replaceAll('&amp;', '&').replaceAll('LOCALE', locale);
    let passwordResetLink = window.SETTINGS.config.passwordResetLink.replaceAll('&amp;', '&').replaceAll('LOCALE', locale);
    // console.log("passwordResetLink: " + passwordResetLink)
    // console.log("signupLink: " + signupLink)
    var forCustomLinksElement = document.getElementById("api");
    let dividerText = '';
    let dontHaveAccountText = '';
    let signUpNowText = '';
    let forgotPasswordText = '';
    let resetPasswordText = '';
    let ieWarningText = '';

    if (locale == 'en-US') {
        dividerText = "OR";
        dontHaveAccountText = "Don\'t have an account?";
        signUpNowText = "Sign up now";
        forgotPasswordText = "Forgot your password?";
        resetPasswordText = "Reset Password";
        ieWarningText = "Internet Explorer 11 is being fully retired by Microsoft, and is no longer a fully supported internet browser. Applicants currently using Internet Explorer 11 may have a degraded experience or be unable to complete the application process, and should transition to Microsoft Edge."
    }
    else if (locale == 'ja-JP') {
        dividerText = "または";
        dontHaveAccountText = "アカウントをお持ちでない場合は、";
        signUpNowText = "今すぐサインアップ";
        forgotPasswordText = "パスワードをお忘れですか？";
        resetPasswordText = "パスワードを再設定する";
        ieWarningText = "Internet Explorer 11 は Microsoft によって完全に廃止され、完全にサポートされているインターネット ブラウザではなくなりました。現在 Internet Explorer 11 を使用している応募者は、エクスペリエンスが低下したり、応募プロセスを完了できない可能性があるため、Microsoft Edge に移行する必要があります。"
    }
    else if (locale == 'ko-KR') {
        dividerText = "또는";
        dontHaveAccountText = "계정이 없나요?";
        signUpNowText = "지금 등록하세요";
        forgotPasswordText = "비밀번호를 잊어 버렸습니까?";
        resetPasswordText = "암호를 재설정";
        ieWarningText = "Internet Explorer 11은 Microsoft에서 완전히 폐기되었으며 더 이상 완전히 지원되는 인터넷 브라우저가 아닙니다. 현재 Internet Explorer 11을 사용하는 신청자는 성능이 저하되거나 신청 프로세스를 완료하지 못할 수 있으므로 Microsoft Edge로 전환해야 합니다."
    }
    else if (locale == 'id-ID') {
        dividerText = "ATAU";
        dontHaveAccountText = "Belum punya akun?";
        signUpNowText = "Daftar sekarang";
        forgotPasswordText = "Lupa kata sandi Anda?";
        resetPasswordText = "Atur Ulang Kata Sandi";
        ieWarningText = "Internet Explorer 11 sepenuhnya dihentikan oleh Microsoft, dan tidak lagi menjadi browser internet yang didukung sepenuhnya. Pemohon yang saat ini menggunakan Internet Explorer 11 mungkin mengalami kendala atau tidak dapat menyelesaikan proses aplikasi, dan harus beralih ke Microsoft Edge."
    }
    else if (locale == 'ms-MY') {
        dividerText = "ATAU";
        dontHaveAccountText = "Belum mempunyai akaun?";
        signUpNowText = "Daftar sekarang";
        forgotPasswordText = "Lupa kata laluan anda?";
        resetPasswordText = "Tetapkan Semula Kata Laluan";
        ieWarningText = "Internet Explorer 11 akan dihentikan sepenuhnya oleh Microsoft dan bukan lagi penyemak imbas internet yang disokong sepenuhnya. Pemohon yang sedang menggunakan Internet Explorer 11 mungkin mengalami pengalaman yang kurang baik atau tidak dapat menyelesaikan proses permohonan, dan harus beralih kepada Microsoft edge."
    }
    else if (locale == 'zh-TW') {
        dividerText = "或者";
        dontHaveAccountText = "沒有帳戶？";
        signUpNowText = "立即註冊";
        forgotPasswordText = "忘記密碼了嗎？";
        resetPasswordText = "重設密碼";
        ieWarningText = "Internet Explorer 11已被微軟淘汰，不再支持的互聯網瀏覽器。 當前使用Internet Explorer 11的申請人，可能體驗不佳或無法完成申請過程，請轉換為 Microsoft Edge。"
    }
    else if (locale == 'zh-CN') {
        dividerText = "或者";
        dontHaveAccountText = "还未建立档案?";
        signUpNowText = "立即注册";
        forgotPasswordText = "忘记密码?";
        resetPasswordText = "重设密码";
        ieWarningText = "Internet Explorer 11 已被 Microsoft 全面淘汰，不再是支持的互联网游览器。使用Internet Explorer 11的申请人可能面临较差的体验或无法完成申请流程，因此建议使用 Microsoft Edge。"
    }
    else if (locale == 'th-TH') {
        dividerText = "หรือ";
        dontHaveAccountText = "ยังไม่มีบัญชี?";
        signUpNowText = "สร้างผู้ใช้งานใหม่ตอนนี้";
        forgotPasswordText = "ลืมรหัสผ่านหรือไม่?";
        resetPasswordText = "รีเซ็ตรหัสผ่าน";
        ieWarningText = "Internet Explorer 11 ได้ถูกยุติการใช้งาน และไม่ใช่เบราว์เซอร์ที่ได้รับการสนับสนุนโดย Microsoft อีกต่อไป ผู้สมัครที่ใช้ Internet Explorer 11 ในปัจจุบันอาจประสบปัญหาในการใช้งานการหรือไม่สามารถดำเนินการตามขั้นตอนการสมัครให้เสร็จสมบูรณ์ได้ และควรเปลี่ยนไปใช้ Microsoft Edge"
    }
    else if (locale == 'vi-VN') {
        dividerText = "HOẶC";
        dontHaveAccountText = "Chưa có tài khoản?";
        signUpNowText = "Đăng ký tài khoản ngay bây giờ";
        forgotPasswordText = "Quên mật khẩu?";
        resetPasswordText = "Đặt lại mật khẩu";
        ieWarningText = "Microsoft sẽ ngừng vận hành Internet Explorer 11 và sẽ không còn là trình duyệt Internet được hỗ trợ đầy đủ nữa. Đương đơn hiện đang sử dụng Internet Explorer 11 có thể có gặp trở ngại hoặc không thể hoàn tất quy trình tạo hồ sơ thị thực. Quý vị nên chuyển sang trình duyệt Microsoft Edge."
    }
    else if (locale == 'fil-PH') {
        dividerText = "O";
        dontHaveAccountText = "Wala pang account?";
        signUpNowText = "Mag sign-up nagyoun";
        forgotPasswordText = "Nakalimutan ang iyong password?";
        resetPasswordText = "I-reset ang Password";
        ieWarningText = "Ang Internet Explorer 11 ay ganap ng na retiro ng Microsoft, at hindi na buong sinusuportahan ng internet browser. Ang mga aplikante na gumagamit ng Internet Explorer 11 ay maaaaring mag karoon ng mababang karanasan o hindi makapag kumpleto ng application process ay dapat na lumipat sa Microsoft Edge. "
    }
    else if (locale == 'lo-LA') {
        dividerText = "ຫຼື";
        dontHaveAccountText = "ຍັງບໍ່ມີບັນຊີບໍ?";
        signUpNowText = "ລົງທະບຽນດຽວນີ້";
        forgotPasswordText = "ລືມລະຫັດຜ່ານຂອງທ່ານບໍ?";
        resetPasswordText = "ຣີເຊັດລະຫັດຜ່ານ";
        ieWarningText = "Microsoft ກໍາລັງຖືກຢຸດໃຫ້ບໍລິການ Internet Explorer 11 ໂດຍສົມບູນ ແລະ ບໍ່ແມ່ນໂປຣແກຕົວທ່ອງເວັບອິນເຕີເນັດທີ່ໄດ້ຮັບການສະໜັບສະໜູນຢ່າງເຕັມທີ່ອີກຕໍ່ໄປ. ຜູ້ສະໝັກທີ່ກໍາລັງໃຊ້ Internet Explorer 11 ໃນປະຈຸບັນອາດຈະມີປະສົບການທີ່ເສື່ອມໂຊມ ຫຼື ບໍ່ສາມາດດໍາເນີນຂັ້ນຕອນການສະໝັກໃຫ້ສໍາເລັດໄດ້ ແລະ ຄວນປ່ຽນໄປໃຊ້ Microsoft Edge."
    }
    else if (locale == 'km-KH') {
        dividerText = "ឬ";
        dontHaveAccountText = "មិនមានគណនីមែនទេ?";
        signUpNowText = "បង្កើតឥឡូវនេះ";
        forgotPasswordText = "ភ្លេចពាក្យសម្ងាត់របស់អ្នកមែនទេ?";
        resetPasswordText = "កំណត់ពាក្យសម្ងាត់ឡើងវិញ";
        ieWarningText = "កម្មវិធីរុករកនៅលើអ៊ីនធឺណិត 11 (Internet Explorer 11) កំពុងត្រូវបានក្រុមហ៊ុន Microsoft លែងឱ្យប្រើប្រាស់ទាំងស្រុង ហើយមិនមែនជាកម្មវិធីរុករកអ៊ីនធឺណិតដែលគាំទ្រពេញលេញទៀតទេ។ អ្នកដាក់ពាក្យដែលកំពុងប្រើកម្មវិធី Internet Explorer 11 អាចមានបទពិសោធមិនសូវពេញចិត្ត ឬមិនអាចបញ្ចប់ដំណើរការកម្មវិធីបាន ហើយគួរតែប្តូរទៅប្រើកម្មវិធី Microsoft Edge វិញ។"
    }
    else if (locale == 'ca-ES') {
        dividerText = "LOS SIS";
        dontHaveAccountText = "Puas yog tsis muaj tus as khauj?";
        signUpNowText = "Rau npe tam sim no";
        forgotPasswordText = "Puas yog tsis nco qab tus zauv zais?";
        resetPasswordText = "Rov Teeb Tus Zauv Zais Dua Tshiab";
        ieWarningText = "Microsoft tau tso tseg tsis siv Internet Explorer 11 thiab tsis muaj kev siv plaus xawj is taws nem uas tau txais kev txhawb nqa ntxiv lawm. Tej zaum cov neeg thov uas siv Internet Explorer 11 nyob rau tam sim no yuav muaj kev paub dhau los tsis zoo los sis tsis tuaj yeem ua kom tiav txheej txheem kev thov, thiab tsim nyog hloov mus rau Microsoft Edge."
    }
    else if (locale == 'ar-SA') {
        dividerText = "أو";
        dontHaveAccountText = "أليس لديك حساب؟ ";
        signUpNowText = "سجّل الآن";
        forgotPasswordText = "هل نسيت كلمة المرور؟ ";
        resetPasswordText = "إعادة تعيين كلمة المرور";
        ieWarningText = "أنهت Microsoft دعمها للمتصفح Internet Explorer 11 تمامًا، ولم يعد متصفح إنترنت مدعومًا بالكامل. قد يتعرض مقدمو الطلبات الذين يستخدمون Internet Explorer 11 حاليًا إلى تجربة ضعيفة أو ربما لا يستطيعون إكمال عملية تقديم الطلب، وعليهم استخدام Microsoft Edge."
    }
    else if (locale == 'cs-CZ') {
        dividerText = "NEBO";
        dontHaveAccountText = "Nemáte účet?";
        signUpNowText = "Zaregistrujte se nyní";
        forgotPasswordText = "Zapomněli jste heslo?";
        resetPasswordText = "Obnovit heslo";
        ieWarningText = "Internet Explorer 11 je společností Microsoft plně vyřazován a již není plně podporovaným internetovým prohlížečem. Žadatelé, kteří v současné době používají Internet Explorer 11, mohou mít zhoršenou uživatelskou zkušenost nebo nemusí být schopni dokončit proces žádosti, a měli by přejít na Microsoft Edge."
    }
    else if (locale == 'da-DK') {
        dividerText = "ELLER";
        dontHaveAccountText = "Har du ikke en konto?";
        signUpNowText = "Tilmeld dig nu";
        forgotPasswordText = "Har du glemt din adgangskode?";
        resetPasswordText = "Nulstil adgangskode";
        ieWarningText = "Internet Explorer 11 bruges ikke længere af Microsoft og er ikke længere en fuldt understøttet internetbrowser. Ansøgere, der i øjeblikket bruger Internet Explorer 11, kan have en forringet oplevelse eller være ude af stand til at fuldføre ansøgningsprocessen og bør skifte til Microsoft Edge."
    }
    else if (locale == 'et-EE') {
        dividerText = "VÕI";
        dontHaveAccountText = "Kas teil pole kontot?";
        signUpNowText = "Registreeruge kohe";
        forgotPasswordText = "Kas unustasite parooli?";
        resetPasswordText = "Parooli lähtestamine";
        ieWarningText = "Microsoft on Internet Explorer 11 täielikult pensionile jätnud ega ole enam täielikult toetatud internetibrauser. Internet Explorer 11 kasutavad taotlejad võivad olla halvenenud kogemusega või ei saa taotlusprotsessi lõpule viia ja peaksid üle minema Microsoft Edge'ile."
    }
    else if (locale == 'fi-FI') {
        dividerText = "TAI";
        dontHaveAccountText = "Eikö sinulla ole tiliä?";
        signUpNowText = "Rekisteröidy nyt";
        forgotPasswordText = "Unohditko salasanasi?";
        resetPasswordText = "Nollaa salasana";
        ieWarningText = "Microsoft on poistanut Internet Explorer 11:n käytöstä, eikä se ole enää täysin tuettu Internet-selain. Tällä hetkellä Internet Explorer 11 -selainta käyttävien hakijoiden käyttökokemus sivustolla voi olla heikko, eivätkä välttämättä voi suorittaa hakuprosessia loppuun, ja heidän on siirryttävä käyttämään Microsoft Edgeä."
    }
    else if (locale == 'fr-FR') {
        dividerText = "OU";
        dontHaveAccountText = "Vous n’avez pas de compte?";
        signUpNowText = "Inscrivez-vous dès maintenant";
        forgotPasswordText = "Vous avez oublié votre mot de passe?";
        resetPasswordText = "Réinitialiser le mot de passe";
        ieWarningText = "Internet Explorer 11 est entièrement retiré de la vente par Microsoft et n’est plus un navigateur Internet entièrement pris en charge. Les demandeurs utilisant actuellement Internet Explorer 11 peuvent avoir une mauvaise expérience ou ne pas être en mesure de terminer le processus de demande, et doivent passer à Microsoft Edge."
    }
    else if (locale == 'de-DE') {
        dividerText = "ODER";
        dontHaveAccountText = "Sie haben noch kein Konto?";
        signUpNowText = "Jetzt anmelden";
        forgotPasswordText = "Sie haben Ihr Passwort vergessen?";
        resetPasswordText = "Passwort zurücksetzen";
        ieWarningText = "Der Internet Explorer 11 wird von Microsoft vollständig aus dem Verkehr gezogen und ist kein voll unterstützter Internet-Browser mehr. Antragsteller, die derzeit den Internet Explorer 11 verwenden, können das Antragsverfahren ggf. nur eingeschränkt oder gar nicht abschließen und sollten auf Microsoft Edge umsteigen."
    }
    else if (locale == 'he-IL') {
        dividerText = "או";
        dontHaveAccountText = "אין לך חשבון?";
        signUpNowText = "יש להירשם עכשיו";
        forgotPasswordText = "שכחת סיסמה?";
        resetPasswordText = "איפוס סיסמה";
        ieWarningText = "Internet Explorer 11 יוצא לגמרי משירות על-ידי Microsoft, וכבר אינו דפדפן אינטרנט שנתמך במלואו. מועמדים המשתמשים כעת ב-Internet Explorer 11 עשויים לחוות חוויה פחותה או שלא יוכלו להשלים את תהליך הבקשה, ועליהם לעבור ל-Microsoft Edge."
    }
    else if (locale == 'hu-HU') {
        dividerText = "VAGY";
        dontHaveAccountText = "Nincs fiókja?";
        signUpNowText = "Regisztráljon most";
        forgotPasswordText = "Elfelejtette a jelszavát?";
        resetPasswordText = "Jelszó visszaállítása";
        ieWarningText = "Az Internet Explorer 11-et a Microsoft teljes mértékben visszavonta, és már nem teljes mértékben támogatott internetböngésző. A jelenleg Internet Explorer 11-et használó kérelmezőknél előfordulhat, hogy az élmény romlik, vagy nem tudják elvégezni a kérelmezési folyamatot, ezért át kell térniük a Microsoft Edge-re."
    }
    else if (locale == 'lv-LV') {
        dividerText = "VAI";
        dontHaveAccountText = "Vai Jums nav konta?";
        signUpNowText = "Reģistrēties tagad";
        forgotPasswordText = "Vai aizmirsāt paroli?";
        resetPasswordText = "Atiestatīt paroli";
        ieWarningText = "Microsoft pilnībā pārtrauc Internet Explorer 11 darbību, un tā vairs nav pilnībā atbalstīta interneta pārlūkprogramma. Pretendentiem, kas pašlaik izmanto pārlūkprogrammu Internet Explorer 11, var pasliktināties lietošanas vai nebūs iespējams pabeigt pieteikšanās procesu, un viņiem ir jāpāriet uz programmu Microsoft Edge."
    }
    else if (locale == 'pt-BR') {
        dividerText = "ILI";
        dontHaveAccountText = "Nemate nalog?";
        signUpNowText = "Registrujte se sada";
        forgotPasswordText = "Zaboravili ste lozinku?";
        resetPasswordText = "Resetujte lozinku";
        ieWarningText = "Microsoft u potpunosti povlači Internet Explorer 11, pa on više nije u potpunosti podržan internet pretraživač. Podnosioci aplikacija koji trenutno koriste Internet Explorer 11 mogu imati lošije iskustvo ili neće biti u mogućnosti da završe proces aplikacije, pa bi trebalo da pređu na Microsoft Edge."
    }
    else if (locale == 'nb-NO') {
        dividerText = "ELLER";
        dontHaveAccountText = "Har du ikke konto?";
        signUpNowText = "Registrer deg nå";
        forgotPasswordText = "Har du glemt passordet ditt?";
        resetPasswordText = "Tilbakestill passord";
        ieWarningText = "Internet Explorer 11 er trukket tilbake av Microsoft, og er ikke lenger en støttet nettleser. Søkere som bruker Internet Explorer 11, kan ha en forringet opplevelse eller være ute av stand til å fullføre søknadsprosessen, og bør gå over til Microsoft Edge."
    }
    else if (locale == 'sk-SK') {
        dividerText = "ALEBO";
        dontHaveAccountText = "Nemáte účet?";
        signUpNowText = "Zaregistrovať sa teraz";
        forgotPasswordText = "Zabudli ste heslo?";
        resetPasswordText = "Obnoviť heslo";
        ieWarningText = "Spoločnosť Microsoft úplne ukončuje podporu prehliadača Internet Explorer 11 a už to nie je plne podporovaný internetový prehliadač. Žiadatelia, ktorí v súčasnosti používajú Internet Explorer 11, môžu mať zhoršený zážitok alebo nemusia byť schopní dokončiť proces podávania žiadosti a mali by prejsť na Microsoft Edge."
    }
    else if (locale == 'sv-SE') {
        dividerText = "ELLER";
        dontHaveAccountText = "Har du inget konto?";
        signUpNowText = "Registrera dig nu";
        forgotPasswordText = "Glömt ditt lösenord?";
        resetPasswordText = "Återställ lösenord";
        ieWarningText = "Internet Explorer 11 har tagits ur bruk av Microsoft, och är inte längre en webbläsare med fullt stöd. Sökande som för närvarande använder Internet Explorer 11 kan få en försämrad upplevelse och kanske inte kan slutföra ansökningsprocessen och bör övergå till Microsoft Edge."
    }
    else if (locale == 'zh-HK') {
        dividerText = "或";
        dontHaveAccountText = "沒有賬號";
        signUpNowText = "立即註冊";
        forgotPasswordText = "忘記密碼？";
        resetPasswordText = "重設密碼";
        ieWarningText = "Microsoft 已完全取消了 Internet Explorer 11，不再全面支持該瀏覽器。目前使用 Internet Explorer 11的申請人可能會遇到網站性能下降或無法完成申請流程，應轉換到 Microsoft Edge 瀏覽器e。"
    }
    else if (locale == 'mn-Mong-CN') {
        dividerText = "ЭСВЭЛ";
        dontHaveAccountText = "Бүртгэл байхгүй бол?";
        signUpNowText = "Одоо бүртгүүлэх";
        forgotPasswordText = "Нууц үгээ мартсан бол?";
        resetPasswordText = "Нууц үг сэргээх";
        ieWarningText = "Internet Explorer 11-ийг Майкрософт бүрэн ашиглахаа больсон бөгөөд бүрэн дэмждэг интернэт хөтөч байхаа больсон. Одоогоор Internet Explorer 11 ашиглаж байгаа виз мэдүүлэгчийн хувьд хэрэглэхэд төвөгтэй, эсвэл хүндрэл гарч виз мэдүүлгийн үйл явцыг дуусгах боломжгүй байж магадгүй тул Microsoft Edge руу шилжих шаардлагатай."
    }
    else if (locale == 'it-IT') {
        dividerText = "বা";
        dontHaveAccountText = "কোনও অ্যাকাউন্ট নেই?";
        signUpNowText = "এখনই সাইন আপ করুন";
        forgotPasswordText = "আপনার পাসওয়ার্ড ভুলে গেছেন?";
        resetPasswordText = "পাসওয়ার্ড রিসেট করুন";
        ieWarningText = "ইন্টারনেট এক্সপ্লোরার ১১ মাইক্রোসফ্ট দ্বারা সম্পূর্ণরূপে খারিজ করা হচ্ছে এবং এটি আর সম্পূর্ণরূপে সমর্থিত ইন্টারনেট ব্রাউজার নয়। বর্তমানে ইন্টারনেট এক্সপ্লোরার ১১ ব্যবহার করে এমন আবেদনকারীদের একটি অবনমিত অভিজ্ঞতা থাকতে পারে অথবা আবেদন প্রক্রিয়া সম্পূর্ণ করতে অক্ষম হতে পারেন, এবং তাদের মাইক্রোসফ্ট এজে স্থানান্তরিত হওয়া উচিত।"
    }
    else if (locale == 'pt-PT') {
        dividerText = "वा";
        dontHaveAccountText = "खाता छैन ?";
        signUpNowText = "अहिले नै साइन अप गर्नुहोस्";
        forgotPasswordText = "पासवर्ड बिर्सनुभयो ?";
        resetPasswordText = "पासवर्ड रिसेट";
        ieWarningText = "माइक्रोसफ्टले इन्टरनेट एक्सप्लोरर 11 को प्रयोग पूर्ण रूपमा बन्द गरिसकेको छ र अब यो पूर्ण रूपमा समर्थित इन्टरनेट ब्राउजर होइन । हाल इन्टरनेट एक्सप्लोरर 11 प्रयोग गर्ने आवेदकहरूले निकृष्ट अनुभव गर्न सक्ने छन् वा आवेदन प्रक्रिया पूरा गर्न असमर्थ हुन सक्ने छन् र माइक्रोसफ्ट एज प्रयोग गर्नुपर्ने हुन्छ ।"
    }
    else if (locale == 'sr-Cyrl-CS') {
        dividerText = "නැතහොත්";
        dontHaveAccountText = "ගිණුමක් නැති ද? ";
        signUpNowText = "දැන් ලියාපදිංචි වන්න";
        forgotPasswordText = "ඔබේ මුරපදය අමතක වුණා ද?";
        resetPasswordText = "මුරපදය යළි සකසන්න";
        ieWarningText = "Internet Explorer 11 Microsoft මගින් සම්පූර්ණයෙන් ම නතර කර දමනු ලබන අතර තවදුරටත් එය සම්පූර්ණයෙන් සහාය දක්වනු ලබන අන්තර්ජාල බ්රවුසරයක් නොවේ. දැනට Internet Explorer 11 භාවිත කරන අයැදුම්කරුවන්ට ගුණත්වයෙන් අඩු අත්දැකීමක් ලැබීමට හෝ අයැදුම් කිරීමේ ක්රියාවලිය සම්පූර්ණ කිරීමට අපොහොසත් වීමට ඉඩ ඇති අතර ඔවුන් Microsoft Edge වෙත මාරු විය යුතු ය."
    }
    else if (locale == 'sl-SI') {
        dividerText = "அல்லது";
        dontHaveAccountText = "கணக்கு இல்லையா?";
        signUpNowText = "இப்போதே பதிவு செய்க";
        forgotPasswordText = "உங்கள் கடவுச்சொல் மறந்துவிட்டதா?";
        resetPasswordText = "கடவுச்சொல்லை மீட்டமைக்கவும்";
        ieWarningText = "இன்டர்நெட் எக்ஸ்ப்ளோரர் 11 மைக்ரோசாப்ட் நிறுவனத்தால் முழுமையாக ஓய்வு பெறுகிறது, மேலும் இது இனி முழுமையாக ஆதரிக்கப்படும் இணைய உலாவி அல்ல. தற்போது இன்டர்நெட் எக்ஸ்பிளோரர் 11 ஐப் பயன்படுத்தும் விண்ணப்பதாரர்களுக்கு மோசமான அனுபவம் இருக்கலாம் அல்லது விண்ணப்ப செயல்முறையை முடிக்க முடியாமல் போகலாம், அவர்கள் மைக்ரோசாஃப்ட் எட்ஜ்-க்கு மாற வேண்டும்."
    }
    else if (locale == 'hi-IN') {
        dividerText = "یا";
        dontHaveAccountText = "کیا آپ کا اکاؤنٹ نہیں ہے؟ ";
        signUpNowText = "ابھی سائن اپ کریں";
        forgotPasswordText = "کیا آپ اپنا پاس ورڈ بھول گئے ہیں؟";
        resetPasswordText = "پاس ورڈ ری سیٹ کریں";
        ieWarningText = "مائیکروسافٹ نے انٹرنیٹ ایکسپلورر 11 کو مکمل طور پر ریٹائر کر دیا ہے، اور یہ اب ایک مکمل طور پر سپورٹ شدہ انٹرنیٹ براؤزر نہیں ہے۔ جو درخواست گزار اس وقت انٹرنیٹ ایکسپلورر 11 استعمال کر رہے ہیں، انہیں کمزور تجربے کا سامنا کرنا پڑ سکتا ہے یا وہ درخواست کے عمل کو مکمل کرنے میں ناکام ہو سکتے ہیں، اور انہیں مائیکروسافٹ ایج پر منتقل ہونا چاہیے۔"
    }
    else if (locale == 'ru-RU') {
        dividerText = "ИЛИ";
        dontHaveAccountText = "Нет учетной записи?";
        signUpNowText = "Зарегистрироваться сейчас";
        forgotPasswordText = "Забыли пароль?";
        resetPasswordText = "Сбросить пароль";
        ieWarningText = "Microsoft полностью прекращает поддержку Internet Explorer 11, и данный браузер больше не поддерживается. Пользователи данного браузера могут столкнуться с ухудшением качества работы или не смогут завершить процесс подачи заявления на визу, и им следует перейти на Microsoft Edge."
    }
    else if (locale == 'bg-BG') {
        dividerText = "ИЛИ";
        dontHaveAccountText = "Нямате профил?";
        signUpNowText = "Регистрирайте се сега";
        forgotPasswordText = "Забравена парола";
        resetPasswordText = "Възстановяване на паролата";
        ieWarningText = "Internet Explorer 11 се изтегля напълно от Microsoft и вече не е напълно поддържан интернет браузър. Кандидатите, които в момента използват Internet Explorer 11, могат да изпитат затруднения или да не могат да завършат процеса на кандидатстване и трябва да преминат към Microsoft Edge.";
    }
    else if (locale == 'hr-HR') {
        dividerText = "ILI";
        dontHaveAccountText = "Nemate račun?";
        signUpNowText = "Registriraj se sada";
        forgotPasswordText = "Zaboravili ste lozinku?";
        resetPasswordText = "Ponovno postavi lozinku";
        ieWarningText = "Microsoft u potpunosti ukida Internet Explorer 11 te on više nije potpuno podržan internetski preglednik. Podnositeljima zahtjeva koji trenutačno upotrebljavaju Internet Explorer 11 možda će biti otežan postupak podnošenja zahtjeva ili ga neće moći dovršiti te bi trebali prijeći na Microsoft Edge.";
    }
    else if (locale == 'sq-AL') {
        dividerText = "OSE";
        dontHaveAccountText = "Nuk keni një llogari?";
        signUpNowText = "Regjistrohuni tani";
        forgotPasswordText = "Keni harruar fjalëkalimin?";
        resetPasswordText = "Rivendosni fjalëkalimin";
        ieWarningText = "Microsoft-i po e nxjerr thuajse jashtë përdorimi Internet Explorer 11 dhe nuk është më një shfletues interneti i mbështetur plotësisht. Aplikantët që po përdorin aktualisht Internet Explorer 11 mund të kenë një përvojë të kufizuar ose mund të mos e përfundojnë procesin e aplikimit dhe duhet të kalojnë në Microsoft Edge."
    }
    else if (locale == 'hy-AM') {
        dividerText = "ԿԱՄ";
        dontHaveAccountText = "Չունե՞ք հաշիվ։";
        signUpNowText = "Գրանցվել հիմա";
        forgotPasswordText = "Մոռացե՞լ եք ձեր գաղտնաբառը։";
        resetPasswordText = "Վերակայել գաղտնաբառը";
        ieWarningText = "Internet Explorer 11-ն ամբողջությամբ հեռացվում է Microsoft-ի կողմից և այլևս ամբողջությամբ աջակցվող ինտերնետային զննիչ չէ: Ներկայումս Internet Explorer 11-ն օգտագործող դիմումատուները կարող են բախվել ծառայության որակի վատթարացման հետ կամ չկարողանալ ավարտել դիմումի գործընթացը և պետք է անցնեն Microsoft Edge:"
    }
    else if (locale == 'az-Cyrl-AZ') {
        dividerText = "VƏ YA";
        dontHaveAccountText = "Hesabınız yoxdur?";
        signUpNowText = "İndi qeydiyyatdan keçin";
        forgotPasswordText = "Parolunuzu unutmusunuz?";
        resetPasswordText = "Parolu yeniləyin";
        ieWarningText = "Microsoft, Internet Explorer 11-in istifadəsini tamamilə dayandırır və artıq o, tam dəstəklənən internet brauzeri deyil. Hazırda Internet Explorer 11-dən istifadə edən ərizəçilər sistemdə çətinliyə məruz qala və ya viza müraciəti prosesini başa çatdırmaqda problem yaşaya biləcəyi üçün Microsoft Edge-ə keçməlidir."
    }
    else if (locale == 'bs-Latn-BA') {
        dividerText = "ILI";
        dontHaveAccountText = "Nemate korisnički nalog?";
        signUpNowText = "Registrujte se sada";
        forgotPasswordText = "Zaboravili ste lozinku?";
        resetPasswordText = "Resetovanje lozinke";
        ieWarningText = "Microsoft u potpunosti ukida Internet Explorer 11 koji više nije podržan kao internet preglednik. Kandidati koji trenutno koriste Internet Explorer 11 mogu imati lošije korisničko iskustvo ili neće moći završiti proces prijave, te bi trebali preći na Microsoft Edge."
    }
    else if (locale == 'fa-IR') {
        dividerText = "یا";
        dontHaveAccountText = "حساب کاربری ندارید؟";
        signUpNowText = "هم‌اکنون ثبت نام کنید";
        forgotPasswordText = "کلمه عبورتان را فراموش کرده‌اید؟";
        resetPasswordText = "بازنشانی کلمه عبور";
        ieWarningText = "Internet Explorer 11 کاملاً توسط Microsoft منسوخ شده و دیگر یک مرورگر اینترنتی کاملاً پشتیبانی‌شده نیست. متقاضی‌هایی که در حال حاضر از Internet Explorer 11 استفاده می‌کنند ممکن است تجربه تنزل‌یافته‌ای داشته باشند یا نتوانند فرآیند درخواست را تکمیل کنند و باید از Microsoft Edge استفاده کنند."
    }
    else if (locale == 'ka-GE') {
        dividerText = "ან";
        dontHaveAccountText = "ანგარიში არ გაქვთ?";
        signUpNowText = "ახლავე დარეგისტრირდით";
        forgotPasswordText = "პაროლი დაგავიწყდათ?";
        resetPasswordText = "პაროლის გადაყენება";
        ieWarningText = "Microsoft-მა Internet Explorer 11 სრულად ამოიღო და ის სრულად მხარდაჭერილი ინტერნეტის ბრაუზერი აღარ არის. განმცხადებლებს, რომლებიც ამჟამად Internet Explorer 11-ს იყენებენ, შეიძლება სირთულეები გაუჩნდეთ ან  განაცხადის პროცესის დასრულება ვერ შეძლონ. ამიტომ ისინი Microsoft Edge-ზე უნდა გადავიდნენ."
    }
    else if (locale == 'mk-MK') {
        dividerText = "ИЛИ";
        dontHaveAccountText = "Немате сметка?";
        signUpNowText = "Пријавете се сега";
        forgotPasswordText = "Ја заборавивте лозинката?";
        resetPasswordText = "Ресетирајте лозинка";
        ieWarningText = "Internet Explorer 11 е целосно повлечен од Microsoft и повеќе не е целосно поддржан пребарувач за интернет. Апликантите што тековно користат Internet Explorer 11 може да имаат нарушено искуство или нема да може да го комплетираат процесот за апликацијата и треба да се префрлат на Microsoft Edge."
    }
    else if (locale == 'el-GR') {
        dividerText = "Ή";
        dontHaveAccountText = "Δεν έχετε λογαριασμό;";
        signUpNowText = "Εγγραφή τώρα";
        forgotPasswordText = "Ξεχάσατε τον κωδικό πρόσβασής σας;";
        resetPasswordText = "Επαναφορά κωδικού πρόσβασης";
        ieWarningText = "Το Internet Explorer 11 αποσύρεται πλήρως από τη Microsoft και δεν είναι πλέον ένα πλήρως υποστηριζόμενο πρόγραμμα περιήγησης στο διαδίκτυο. Οι αιτούντες που χρησιμοποιούν επί του παρόντος το Internet Explorer 11 ενδέχεται να έχουν μειωμένη εμπειρία ή να μην μπορούν να ολοκληρώσουν τη διαδικασία υποβολής αίτησης και θα πρέπει να μεταβούν στο Microsoft Edge."
    }
    else if (locale == 'lt-LT') {
        dividerText = "ARBA";
        dontHaveAccountText = "Neturite paskyros?";
        signUpNowText = "Registruotis";
        forgotPasswordText = "Pamiršote slaptažodį?";
        resetPasswordText = "Iš naujo nustatyti slaptažodį";
        ieWarningText = "„Microsoft“ pilnai nutraukia „Internet Explorer 11“ palaikymą, todėl ši naršyklė jau nebėra pilnai palaikoma. Šiuo metu „Internet Explorer 11“ naudojančių pareiškėjų naudojimasis portalu gali sutrikti arba jiems gali nepavykti sėkmingai užbaigti paraiškos teikimo proceso, todėl reikėtų pereiti prie naršyklės „Microsoft Edge“."
    }
    else if (locale == 'pl-PL') {
        dividerText = "LUB";
        dontHaveAccountText = "Nie masz konta?";
        signUpNowText = "Zarejestruj się teraz";
        forgotPasswordText = "Zapomniałeś hasła?";
        resetPasswordText = "Zresetuj hasło";
        ieWarningText = "Internet Explorer 11 jest całkowicie wycofywany przez Microsoft i nie jest już w pełni obsługiwaną przeglądarką internetową. Wnioskodawcy korzystający obecnie z przeglądarki Internet Explorer 11 mogą mieć doświadczyć gorszych doświadczeń lub nie być w stanie ukończyć procesu aplikacji i powinni przejść na Microsoft Edge."
    }
    else if (locale == 'ro-RO') {
        dividerText = "SAU";
        dontHaveAccountText = "Nu aveți un cont?";
        signUpNowText = "Înregistrare";
        forgotPasswordText = "V-ați uitat parola?";
        resetPasswordText = "Resetare parolă";
        ieWarningText = "Internet Explorer 11 este scos complet din uz de Microsoft și nu mai este un browser complet acceptat. Solicitanții care folosesc Internet Explorer 11 pot întâmpina dificultăți sau nu vor putea finaliza procesul de solicitare și ar trebui să folosească Microsoft Edge."
    }
    else if (locale == 'sr-Latn-CS') {
        dividerText = "ILI";
        dontHaveAccountText = "Nemate nalog?";
        signUpNowText = "Prijavite se odmah";
        forgotPasswordText = "Zaboravili ste lozinku?";
        resetPasswordText = "Resetujte lozinku";
        ieWarningText = "Microsoft je u potpunosti napustio Internet Explorer 11 i to više nije potpuno podržan internet pregledač. Podnosioci zahteva koji trenutno koriste Internet Explorer 11 mogu imati pogoršano iskustvo ili neće moći da završe proces prijave i treba da pređu na Microsoft Edge."
    }
    else if (locale == 'tr-TR') {
        dividerText = "VEYA";
        dontHaveAccountText = "Hesabınız yok mu?";
        signUpNowText = "Şimdi kaydolun";
        forgotPasswordText = "Şifrenizi mi unuttunuz?";
        resetPasswordText = "Şifre Sıfırlama";
        ieWarningText = "Internet Explorer 11, Microsoft tarafından tamamen kullanımdan kaldırılıyor ve artık tam olarak desteklenen bir internet tarayıcısı değil. Şu anda Internet Explorer 11 kullanan başvuru sahipleri, düşük bir deneyime sahip olabilir veya başvuru sürecini tamamlayamayabilir ve Microsoft Edge'e geçiş yapmalıdır."
    }
    else if (locale == 'uk-UA') {
        dividerText = "АБО";
        dontHaveAccountText = "У Вас немає профілю?";
        signUpNowText = "Зареєструватись";
        forgotPasswordText = "Забули пароль?";
        resetPasswordText = "Обновити пароль";
        ieWarningText = "Internet Explorer 11 більше не підтримується як браузер. Заявники, які використовують Internet Explorer 11 можуть мати технічні проблеми із записом і повинні перейти на браузер Microsoft Edge"
    }
    else {
        dividerText = "OR";
        dontHaveAccountText = "Don\'t have an account?";
        signUpNowText = "Sign up now";
        forgotPasswordText = "Forgot your password?";
        resetPasswordText = "Reset Password";
        ieWarningText = "Internet Explorer 11 is being fully retired by Microsoft, and is no longer a fully supported internet browser. Applicants currently using Internet Explorer 11 may have a degraded experience or be unable to complete the application process, and should transition to Microsoft Edge."
    }

    var customLinksElement = '<div class="divider"><h2>' + dividerText + '</h2></div>';
    customLinksElement += '<div id="create">';

    customLinksElement += '<p>' + dontHaveAccountText + ' <a id="signup" href="#">' + signUpNowText + '</a></p>';
    customLinksElement += '<p>' + forgotPasswordText + ' <a id="passwordReset" href="#">' + resetPasswordText + '</a></p>';
    customLinksElement += '</div>';
    customLinksElement += '<div id="ieWarning">';
    customLinksElement += '</br>';
    customLinksElement += '<p style="background-color:red;color:white;padding-left:0.2em;padding-bottom:0.2em;border-radius:0.2em">' + ieWarningText + '</p>';
    customLinksElement += '</div>';
    forCustomLinksElement.insertAdjacentHTML( 'afterend', customLinksElement );

    $('#signup').attr("href", signupLink);
    $('#passwordReset').attr("href", passwordResetLink);
}

/*
* These custom links are specifically set for claiming legacy profiles
*/
function setCustomLinksForClaiming(locale) {
    let signupLink = window.SETTINGS.config.signupLink.replaceAll('&amp;', '&').replaceAll('LOCALE', locale);
    // The B2C does not provide a specific signin link. Hence, passwordResetLink is repurposed to make this work for
    // profile claiming.
    let signinLink = window.SETTINGS.config.passwordResetLink.replaceAll('&amp;', '&').replaceAll('LOCALE', locale);
    var forCustomLinksElement = document.getElementById("api");
    let claimingText = '';
    let signUpNowHeaderText = '';
    let signUpNowText = '';
    let signInHeaderText = '';
    let signInText = '';

    if (locale == 'ar-SA') {
        claimingText = "يرجى من المتقدمين المسجلين في النظام القديم ملء النموذج أدناه:";
        signUpNowHeaderText = "يحتاج المستخدمون الجدد ، وكذلك المتقدمون الذين أكملوا النموذج أعلاه بنجاح ، إلى التسجيل بالنقر فوق الزر أدناه:";
        signUpNowText = " سجل الآن ";
        signInHeaderText = "يمكن للمتقدمين الذين أكملوا التسجيل أعلاه تسجيل الدخول أدناه:";
        signInText = "تسجيل الدخول";

        $("#captcha-content-ar").hide();
        $("#claimprofile-content-ar").show();
    }
    else if (locale == 'he-IL') {
        claimingText = "מועמדים שנרשמו לפני ה-1 לנובמבר, 2024 אנא מלאו את הטופס שלהלן:";
        signUpNowHeaderText = "משתמשים חדשים, וכן מועמדים שהשלימו בהצלחה את הטופס לעיל, נדרשים להירשם על ידי לחיצה על הכפתור שלהלן:";
        signUpNowText = "הירשם עכשיו";
        signInHeaderText = "מועמדים שהשלימו את ההרשמה לעיל, יכולים להתחבר למטה:";
        signInText = "הרשמה";

        $("#captcha-content-he").hide();
        $("#claimprofile-content-he").show();
    }
    else if (locale == 'sq-AL') {
        claimingText = "Aplikantët e regjistruar në sistemin e vjetër, plotësoni formularin e mëposhtëm:";
        signUpNowHeaderText = "Përdoruesit e rinj, si dhe aplikuesit që plotësuan me sukses formularin e mësipërm, duhet të regjistrohen duke klikuar butonin më poshtë:";
        signUpNowText = "Regjistrohuni tani";
        signInHeaderText = "Aplikantët që kanë përfunduar regjistrimin e mësipërm, mund të identifikohen më poshtë:";
        signInText = "Identifikohuni";

        $("#captcha-content-sq-AL").hide();
        $("#claimprofile-content-sq-AL").show();
    }
    else if (locale == 'hy-AM') {
        claimingText = "Հնացած համակարգում գրանցված դիմողները խնդրում ենք լրացնել ստորև ներկայացված ձևաթուղթը.";
        signUpNowHeaderText = "Նոր օգտատերերը, ինչպես նաև այն դիմողները, որոնք բարեհաջող լրացրել են վերոնշյալ ձևաթուղթը, պետք է գրանցվեն՝ սեղմելով ստորև նշված կոճակը.";
        signUpNowText = "Գրանցվել հիմա";
        signInHeaderText = "Վերոնշյալ գրանցված դիմողները կարող են մուտք գործել ստորև.";
        signInText = "Մուտք գործել համակարգ";

        $("#captcha-content-hy-AM").hide();
        $("#claimprofile-content-hy-AM").show();
    }
    else if (locale == 'az-Cyrl-AZ') {
        claimingText = "Köhnə sistemdə qeydiyyatdan keçmiş ərizəçilər aşağıdakı formanı doldurmalıdırlar:";
        signUpNowHeaderText = "Yeni istifadəçilər və yuxarıdakı formanı tamamlamış ərizəçilər aşağıdakı düyməni klikləyərək qeydiyyatdan keçməlidirlər:";
        signUpNowText = "İndi qeydiyyatdan keç";
        signInHeaderText = "Yuxarıdakı qeydiyyat prosesini tamamlayan ərizəçilər aşağıda daxil ola bilərlər:";
        signInText = "Daxil ol";

        $("#captcha-content-az-Cyrl-AZ").hide();
        $("#claimprofile-content-az-Cyrl-AZ").show();
    }
    else if (locale == 'bs-Latn-BA') {
        claimingText = "Podnositelje registrovane u prethodnom sistemu molimo da popune donji obrazac:";
        signUpNowHeaderText = "Novi korisnici, kao i podnositelji koji su uspješno popunili obrazac gore, trebaju da se registruju pritisnuvši dugme ispod:";
        signUpNowText = "Registrujte se sada";
        signInHeaderText = "Podnositelji koji su završili registraciju iznad, mogu se prijaviti ispod:";
        signInText = "Prijaviti se";

        $("#captcha-content-bs-Latn-BA").hide();
        $("#claimprofile-content-bs-Latn-BA").show();
    }
    else if (locale == 'hr-HR') {
        claimingText = "Podnositelje registrovane u prethodnom sustavu molimo popuniti donji obrazac:";
        signUpNowHeaderText = "Novi korisnici, kao i podnositelji koji su uspješno popunili obrazac gore, trebaju se registrovati pritisnuvši gumb dolje:";
        signUpNowText = "Registrujte se sada";
        signInHeaderText = "Podnositelji koji su završili registraciju iznad, mogu se prijaviti ispod:";
        signInText = "Prijaviti se";

        $("#captcha-content-hr-HR").hide();
        $("#claimprofile-content-hr-HR").show();
    }
    else if (locale == 'fa-IR') {
        claimingText = "متقاضیانی که در سامانه قدیمی ثبت‌نام کرده‌اند، لطفاً فرم زیر را پُر کنند:";
        signUpNowHeaderText = "کاربران جدید، و همچنین متقاضیانی که فرم بالا را با موفقیت پُر کرده‌اند، باید با کلیک بر روی دکمه زیر ثبت‌نام کنند:";
        signUpNowText = "اکنون ثبت‌نام کنید";
        signInHeaderText = "متقاضیانی که مرحله ثبت‌نام فوق را تکمیل کرده‌اند، می‌توانند در زیر وارد سیستم شوند:";
        signInText = "ورود به سیستم";

        $("#captcha-content-fa-IR").hide();
        $("#claimprofile-content-fa-IR").show();
    }
    else if (locale == 'mk-MK') {
        claimingText = "Кандидатите регистрирани во стариот систем треба да го пополнат образецот подолу:";
        signUpNowHeaderText = "Новите корисници, како и барателите кои успешно го пополниле образецот погоре, треба да се регистрираат со кликнување на копчето подолу:";
        signUpNowText = "Пријавете се сега";
        signInHeaderText = "Барателите кои го завршиле пријавувањето погоре, можат да се најават подолу:";
        signInText = "Најави се";

        $("#captcha-content-mk-MK").hide();
        $("#claimprofile-content-mk-MK").show();
    }
    else if (locale == 'el-GR') {
        claimingText = "Οι αιτούντες που είναι καταχωρημένοι στο σύστημα παλαιού τύπου, θα πρέπει να συμπληρώσουν την παρακάτω φόρμα:";
        signUpNowHeaderText = "Οι νέοι χρήστες, καθώς και οι αιτούντες που συμπλήρωσαν με επιτυχία την παραπάνω φόρμα, θα πρέπει να εγγραφούν κάνοντας κλικ στο παρακάτω κουμπί:";
        signUpNowText = "Εγγραφή τώρα";
        signInHeaderText = "Οι αιτούντες που ολοκλήρωσαν την εγγραφή παραπάνω, μπορούν να συνδεθούν παρακάτω:";
        signInText = "Είσοδος";

        $("#captcha-content-el-GR").hide();
        $("#claimprofile-content-el-GR").show();
    }
    else if (locale == 'ru-RU') {
        claimingText = "Заявители, зарегистрированные в старой системе, пожалуйста, заполните нижеследующую форму:";
        signUpNowHeaderText = "Новым пользователям, а также заявителям, успешно заполнившим вышеуказанную форму, необходимо зарегистрироваться, нажав кнопку ниже:";
        signUpNowText = "Зарегистрироваться сейчас";
        signInHeaderText = "Заявители, которые зарегистрировались выше, могут войти в учетную запись ниже:";
        signInText = "Войти";

        $("#captcha-content-ru-RU").hide();
        $("#claimprofile-content-ru-RU").show();
    }
    else if (locale == 'sr-Latn-CS') {
        claimingText = "Podnosioce registrovane u prethodnom sistemu molimo da popune donji obrazac:";
        signUpNowHeaderText = "Novi korisnici, kao i podnosioci koji su uspešno popunili obrazac gore, trebaju da se registruju pritisnuvši dugme ispod:";
        signUpNowText = "Registrujte se sada";
        signInHeaderText = "Podnosioci koji su završili registraciju iznad, mogu se prijaviti ispod:";
        signInText = "Prijaviti se";

        $("#captcha-content-sr-Latn-CS").hide();
        $("#claimprofile-content-sr-Latn-CS").show();
    }
    else if (locale == 'tr-TR') {
        claimingText = "Eski sisteme kayıtlı başvuru sahipleri lütfen aşağıdaki formu doldurunuz:";
        signUpNowHeaderText = "Yeni kullanıcılar ve yukarıdaki formu başarıyla dolduran Başvuru Sahiplerinin aşağıdaki butona basarak kaydolmaları gerekmektedir:";
        signUpNowText = "Şimdi kaydolun";
        signInHeaderText = "Yukarıdaki kayıt işlemini tamamlayan başvuru sahipleri aşağıdan giriş yapabilirler:";
        signInText = "Oturum Aç";

        $("#captcha-content-tr-TR").hide();
        $("#claimprofile-content-tr-TR").show();
    }
    else {
        claimingText = "Applicants registered in the legacy system please fill the form below:";
        signUpNowHeaderText = "New users, as well as Applicants who successfully completed the form above, need to sign up by clicking the button below:";
        signUpNowText = "Sign up now";
        signInHeaderText = "Applicants who completed the sign up above, can log in below:";
        signInText = "Sign In";

        $("#captcha-content-en").hide();
        $("#claimprofile-content-en").show();
    }

    var claimHeadingStyleBegin = '<h3>';
    var claimHeadingStyleEnd = '</h3';
    var customLinksElement = '<div class="divider"><h2><br/></h2></div>';
    customLinksElement += '<div id="create">';
    customLinksElement += '<p>' + claimHeadingStyleBegin + signUpNowHeaderText + claimHeadingStyleEnd + '</p><p><a id="signup" href="#">' + signUpNowText + '</a></p>';
    customLinksElement += '<br/>';
    customLinksElement += '<p>' + claimHeadingStyleBegin + signInHeaderText + claimHeadingStyleEnd + '</p><p><a id="signin" href="#">' + signInText + '</a></p>';
    customLinksElement += '</div>';
    forCustomLinksElement.insertAdjacentHTML( 'afterend', customLinksElement );

    $('#signup').attr("href", signupLink);
    $('#signin').attr("href", signinLink);

    var parent = document.getElementById('attributeList');
    var claimingTextElement = '<br/>' + claimHeadingStyleBegin + claimingText + claimHeadingStyleEnd;
    parent.insertAdjacentHTML('beforeBegin', claimingTextElement);
}

function findLocale() {
    let locale_value = '';

    if(location.search) {
        let params = location.search.substring(1);
        let queryString = new URLSearchParams(params);

        for(let pair of queryString.entries()) {
            if (pair[0] == 'ui_locales') {
                locale_value = pair[1];

                break;
            }
        }
    }

    console.log("Navigator Language from Browser:", navigator.language || navigator.userLanguage);
    console.log("Locale Value from URL is: " + locale_value);

    let language = navigator.language || navigator.userLanguage;

    if (locale_value == 'en-US' || (locale_value == '' && language.startsWith('en'))) {
        locale_value = 'en-US';
    }
    else if (locale_value == 'ja-JP' || (locale_value == '' && language.startsWith('ja'))) {
        locale_value = 'ja-JP';
    }
    else if (locale_value == 'ko-KR' || (locale_value == '' && language.startsWith('ko'))) {
        locale_value = 'ko-KR';
    }
    else if (locale_value == 'id-ID' || (locale_value == '' && language.startsWith('id'))) {
        locale_value = 'id-ID';
    }
    else if (locale_value == 'ms-MY' || (locale_value == '' && language.startsWith('ms'))) {
        locale_value = 'ms-MY';
    }
    else if (locale_value == 'zh-TW') {
        locale_value = 'zh-TW';
    }
    else if (locale_value == 'zh-CN') {
        locale_value = 'zh-CN';
    }
    else if (locale_value == 'th-TH' || (locale_value == '' && language.startsWith('th'))) {
        locale_value = 'th-TH';
    }
    else if (locale_value == 'vi-VN' || (locale_value == '' && language.startsWith('vi'))) {
        locale_value = 'vi-VN';
    }
    else if (locale_value == 'eu-ES' || locale_value == 'fil-PH' || (locale_value == '' && language.startsWith('fil'))) {
        locale_value = 'fil-PH';
    }
    else if (locale_value == 'gl-ES' || locale_value == 'lo-LA' || (locale_value == '' && language.startsWith('lo'))) {
        locale_value = 'lo-LA';
    }
    else if (locale_value == 'nl-NL' || locale_value == 'km-KH' || (locale_value == '' && language.startsWith('km'))) {
        locale_value = 'km-KH';
    }
    else if (locale_value == 'ca-ES' || (locale_value == '' && language.startsWith('ca'))) {
        locale_value = 'ca-ES';
    }
    else if (locale_value == 'ar-SA' || (locale_value == '' && language.startsWith('ar'))) {
        locale_value = 'ar-SA';
    }
    else if (locale_value == 'cs-CZ' || (locale_value == '' && language.startsWith('cs'))) {
        locale_value = 'cs-CZ';
    }
    else if (locale_value == 'da-DK' || (locale_value == '' && language.startsWith('da'))) {
        locale_value = 'da-DK';
    }
    else if (locale_value == 'et-EE' || (locale_value == '' && language.startsWith('et'))) {
        locale_value = 'et-EE';
    }
    else if (locale_value == 'fi-FI' || (locale_value == '' && language.startsWith('fi'))) {
        locale_value = 'fi-FI';
    }
    else if (locale_value == 'fr-FR' || (locale_value == '' && language.startsWith('fr'))) {
        locale_value = 'fr-FR';
    }
    else if (locale_value == 'de-DE' || (locale_value == '' && language.startsWith('de'))) {
        locale_value = 'de-DE';
    }
    else if (locale_value == 'he-IL' || (locale_value == '' && language.startsWith('he'))) {
        locale_value = 'he-IL';
    }
    else if (locale_value == 'hu-HU' || (locale_value == '' && language.startsWith('hu'))) {
        locale_value = 'hu-HU';
    }
    else if (locale_value == 'lv-LV' || (locale_value == '' && language.startsWith('lv'))) {
        locale_value = 'lv-LV';
    }
    else if (locale_value == 'pt-BR') {
        locale_value = 'pt-BR';
    }
    else if (locale_value == 'nb-NO' || (locale_value == '' && language.startsWith('nb'))) {
        locale_value = 'nb-NO';
    }
    else if (locale_value == 'sk-SK' || (locale_value == '' && language.startsWith('sk'))) {
        locale_value = 'sk-SK';
    }
    else if (locale_value == 'sv-SE' || (locale_value == '' && language.startsWith('sv'))) {
        locale_value = 'sv-SE';
    }
    else if (locale_value == 'zh-HK') {
        locale_value = 'zh-HK';
    }
    else if (locale_value == 'kk-KZ' || locale_value == 'mn-Mong-CN') {
        locale_value = 'mn-Mong-CN';
    }
    else if (locale_value == 'it-IT') {
        locale_value = 'it-IT';
    }
    else if (locale_value == 'pt-PT') {
        locale_value = 'pt-PT';
    }
    else if (locale_value == 'sr-Cyrl-CS') {
        locale_value = 'sr-Cyrl-CS';
    }
    else if (locale_value == 'sl-SI') {
        locale_value = 'sl-SI';
    }
    else if (locale_value == 'hi-IN') {
        locale_value = 'hi-IN';
    }
    else if (locale_value == 'ru-RU') {
        locale_value = 'ru-RU';
    }
    else if (locale_value == 'bg-BG') {
        locale_value = 'bg-BG';
    }
    else if (locale_value == 'hr-HR') {
        locale_value = 'hr-HR';
    }
    else if (locale_value == 'sq-AL') {
        locale_value = 'sq-AL';
    }
    else if (locale_value == 'hy-AM') {
        locale_value = 'hy-AM';
    }
    else if (locale_value == 'az-Cyrl-AZ') {
        locale_value = 'az-Cyrl-AZ';
    }
    else if (locale_value == 'bs-Latn-BA') {
        locale_value = 'bs-Latn-BA';
    }
    else if (locale_value == 'fa-IR') {
        locale_value = 'fa-IR';
    }
    else if (locale_value == 'ka-GE') {
        locale_value = 'ka-GE';
    }
    else if (locale_value == 'mk-MK') {
        locale_value = 'mk-MK';
    }
    else if (locale_value == 'el-GR') {
        locale_value = 'el-GR';
    }
    else if (locale_value == 'lt-LT') {
        locale_value = 'lt-LT';
    }
    else if (locale_value == 'pl-PL') {
        locale_value = 'pl-PL';
    }
    else if (locale_value == 'ro-RO') {
        locale_value = 'ro-RO';
    }
    else if (locale_value == 'sr-Latn-CS') {
        locale_value = 'sr-Latn-CS';
    }
    else if (locale_value == 'tr-TR') {
        locale_value = 'tr-TR';
    }
    else if (locale_value == 'uk-UA') {
        locale_value = 'uk-UA';
    }
    else {
        locale_value = 'en-US';
    }

    return locale_value;
}